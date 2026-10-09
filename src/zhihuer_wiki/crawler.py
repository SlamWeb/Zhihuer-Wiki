"""Standalone, local-only Zhihu crawl orchestration.

Each accepted item is committed before the next request. Re-running replays source
pagination and merges by (kind, id); it does not trust stale remote page offsets.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from zhihuer_wiki.vendor.crawler.exceptions import CrawlBlocked, CrawlError, SourceFormatChanged
from zhihuer_wiki.vendor.crawler.markdown import item_filename, render_item_markdown
from zhihuer_wiki.vendor.crawler.models import ContentItem, ContentKind, utc_now_iso
from zhihuer_wiki.vendor.crawler.zhihu import ZhihuPublicCrawler, fallback_profile, parse_user_token
from zhihuer_wiki.vendor.crawler.zhihu_browser import ZhihuBrowserCrawler, save_zhihu_session

KINDS = ("answer", "article", "pin")


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _json(path: Path, payload: Any) -> None:
    _atomic_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _raw_path(root: Path, relative: Any) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise CrawlError("Existing manifest contains an invalid relative Markdown path.")
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or path.suffix.lower() != ".md":
        raise CrawlError("Existing manifest contains an invalid relative Markdown path.")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root):
        raise CrawlError("Existing manifest path leaves the raw directory.")
    return resolved


def _load_rows(root: Path, author: str) -> dict[tuple[str, str], dict[str, Any]]:
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    manifest = root / "manifest.jsonl"
    if not manifest.exists():
        # Unindexed Markdown would silently disappear from a subsequent Wiki.
        if root.exists() and any(root.rglob("*.md")):
            raise CrawlError("Existing raw Markdown has no manifest; import it directly or use a new crawl directory.")
        return rows
    try:
        for line in manifest.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict) or row.get("kind") not in KINDS or not str(row.get("id", "")).isdigit():
                raise CrawlError("Existing manifest contains an invalid content identity.")
            if row.get("author_token") not in {None, author}:
                raise CrawlError("Existing raw directory belongs to a different author.")
            if not _raw_path(root, row.get("path")).is_file():
                raise CrawlError("An existing manifest Markdown file is missing.")
            key = (row["kind"], str(row["id"]))
            if key in rows:
                raise CrawlError("Existing manifest contains duplicate content identities.")
            rows[key] = row
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise CrawlError("Existing manifest could not be read safely.") from None
    return rows


def _write_rows(root: Path, rows: dict[tuple[str, str], dict[str, Any]]) -> None:
    text = "".join(json.dumps(rows[key], ensure_ascii=False, sort_keys=True) + "\n" for key in sorted(rows))
    _atomic_text(root / "manifest.jsonl", text)


def _save_item(root: Path, rows: dict[tuple[str, str], dict[str, Any]], item: ContentItem, author: str) -> None:
    if item.kind not in KINDS or not re.fullmatch(r"[0-9]+", item.id) or item.author_token != author:
        raise SourceFormatChanged("A fetched item did not match the requested author or content identity.")
    if not item.content_text.strip():
        raise SourceFormatChanged("A fetched item contained no full source text.")
    parsed = urlparse(item.url)
    allowed = {"www.zhihu.com", "zhuanlan.zhihu.com"}
    if parsed.scheme not in {"http", "https"} or parsed.netloc not in allowed:
        raise SourceFormatChanged("A fetched item contained an unexpected source URL.")
    key = (item.kind, item.id)
    relative = rows[key]["path"] if key in rows else f"{item.kind}/{item_filename(item)}"
    path = _raw_path(root, relative)
    _atomic_text(path, render_item_markdown(item))
    row = item.to_dict()
    row["path"] = path.relative_to(root).as_posix()
    rows[key] = row
    _write_rows(root, rows)


def _error_code(exc: Exception) -> str:
    # Never echo exceptions: browser exceptions can include local paths, headers
    # or storage-state values. Source response bodies are never diagnostic output.
    if isinstance(exc, CrawlBlocked):
        return "blocked"
    if isinstance(exc, SourceFormatChanged):
        return "source_format"
    if isinstance(exc, CrawlError):
        return "unavailable"
    return "request_failed"


def crawl(
    user: str,
    out_dir: str | Path,
    *,
    max_items: int | None = None,
    storage_state: str | Path | None = None,
    kinds: Iterable[ContentKind] = ("answer", "article", "pin"),
    delay_seconds: float = 1.5,
) -> dict[str, Any]:
    """Crawl public full text with an auditable, credential-free summary.

    max_items caps newly fetched unique identities across all requested kinds.
    A limited smoke sample has status='limited' and complete=False. Exhausted
    public API pagination is required to claim a complete author crawl.
    """
    author = parse_user_token(user)
    if max_items is not None and (isinstance(max_items, bool) or not isinstance(max_items, int) or max_items <= 0):
        raise ValueError("max_items must be a positive integer or None.")
    if isinstance(delay_seconds, bool) or not isinstance(delay_seconds, (float, int)) or not math.isfinite(delay_seconds) or delay_seconds < 0:
        raise ValueError("delay_seconds must be a finite nonnegative number.")
    requested = tuple(dict.fromkeys(kinds))
    if not requested or any(kind not in KINDS for kind in requested):
        raise ValueError("kinds must contain answer, article or pin.")
    root = Path(out_dir).expanduser().resolve()
    state = Path(storage_state).expanduser().resolve() if storage_state is not None else None
    if state is not None and state.is_relative_to(root):
        raise ValueError("Browser credentials must be stored outside the raw output directory.")
    rows = _load_rows(root, author)
    original = set(rows)
    seen: set[tuple[str, str]] = set()
    root.mkdir(parents=True, exist_ok=True)
    profile_path = root / "profile.json"
    profile_data: dict[str, Any] = fallback_profile(author).to_dict()
    if profile_path.exists():
        try:
            existing = json.loads(profile_path.read_text(encoding="utf-8"))
            if not isinstance(existing, dict) or existing.get("author_token") != author:
                raise CrawlError("Existing raw directory belongs to a different author.")
            profile_data = existing
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise CrawlError("Existing author profile could not be read safely.") from None
    summary: dict[str, Any] = {
        "author": author, "out_dir": str(root), "status": "running", "complete": False,
        "fetched_count": 0, "updated_count": 0, "total_count": len(rows),
        "max_items": max_items, "kinds": {kind: {"status": "not_attempted", "fetched_count": 0, "attempts": []} for kind in requested},
        "warnings": [], "started_at": utc_now_iso(), "checkpoint_path": str(root / "crawl_checkpoint.json"),
        "resume_strategy": "replay_and_merge_by_kind_id",
    }

    def checkpoint() -> None:
        summary["fetched_count"] = len(seen)
        summary["updated_count"] = len(seen & original)
        summary["total_count"] = len(rows)
        _json(root / "crawl_checkpoint.json", summary)

    def remaining() -> int | None:
        return None if max_items is None else max_items - len(seen)

    def consume(crawler: Any, kind: str, strategy: str) -> None:
        result = summary["kinds"][kind]
        before = len(seen)
        iterator = crawler.iter_kind(author, kind=kind, limit=remaining())
        outcome = "request_failed"
        try:
            for item in iterator:
                key = (item.kind, item.id)
                if key in seen:
                    continue
                _save_item(root, rows, item, author)
                seen.add(key)
                result["fetched_count"] += 1
                checkpoint()
                if remaining() == 0:
                    outcome = "limited"
                    break
            else:
                outcome = crawler.outcomes.get(kind, "unknown_boundary")
        except Exception as exc:
            outcome = _error_code(exc)
        finally:
            close = getattr(iterator, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    outcome = "browser_cleanup_failed"
        result["attempts"].append({"strategy": strategy, "outcome": outcome, "fetched_count": len(seen) - before})
        result["status"] = "completed" if outcome == "completed" else "limited" if outcome == "limited" else "partial" if result["fetched_count"] else "failed"
        checkpoint()

    if not (root / "manifest.jsonl").exists():
        _write_rows(root, rows)
    checkpoint()
    public = ZhihuPublicCrawler(delay_seconds=delay_seconds, max_api_pages=1000)
    try:
        profile = public.crawl_profile(author)
        if profile.author_token != author:
            raise SourceFormatChanged("Profile identity does not match the requested author.")
        profile_data = {**profile_data, **profile.to_dict()}
    except Exception as exc:
        summary["warnings"].append({"scope": "public_profile", "code": _error_code(exc)})
    _json(profile_path, profile_data)
    needs_browser: list[str] = []
    for kind in requested:
        if remaining() == 0:
            summary["kinds"][kind]["status"] = "limited"
            continue
        consume(public, kind, "public_api")
        result = summary["kinds"][kind]
        if result["status"] not in {"completed", "limited"} or result["fetched_count"] == 0:
            needs_browser.append(kind)
    if needs_browser and remaining() != 0:
        browser = ZhihuBrowserCrawler(storage_state=state, delay_seconds=delay_seconds, max_api_pages=1000)
        for kind in needs_browser:
            if remaining() == 0:
                break
            # A valid empty public API is authoritative even if the optional
            # browser is blocked. Existing content is still never deleted.
            public_empty_complete = summary["kinds"][kind]["status"] == "completed"
            consume(browser, kind, "browser_local_state" if state else "browser_public")
            if public_empty_complete and summary["kinds"][kind]["fetched_count"] == 0:
                summary["kinds"][kind]["status"] = "completed"
    statuses = [summary["kinds"][kind]["status"] for kind in requested]
    if not seen:
        summary["status"] = "failed"
        summary["warnings"].append({"scope": "crawl", "code": "no_source_items_fetched"})
    elif all(status == "completed" for status in statuses):
        summary["status"] = "completed"
        summary["complete"] = True
    elif any(status in {"failed", "partial", "not_attempted"} for status in statuses):
        summary["status"] = "partial"
    else:
        summary["status"] = "limited"
    summary["finished_at"] = utc_now_iso()
    checkpoint()
    _json(root / "crawl_summary.json", summary)
    if not (root / "manifest.jsonl").exists():
        _write_rows(root, rows)
    return summary


def login(storage_state: str | Path) -> dict[str, Any]:
    """Open a visible browser and wait for explicit human login."""
    try:
        save_zhihu_session(Path(storage_state).expanduser().resolve())
    except Exception:
        raise CrawlError("Local Zhihu login did not finish; retry and complete login in the browser.") from None
    return {"status": "saved", "credentials": "local_only"}
