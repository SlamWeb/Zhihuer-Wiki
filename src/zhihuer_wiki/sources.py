"""Immutable Markdown import; no semantic extraction lives in this module."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath

import yaml


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _import_paths(root: Path) -> list[Path]:
    """Crawler manifests are the acceptance boundary, including an empty manifest.

    A crash can leave a Markdown file on disk before its manifest transaction was
    committed. Such orphans must not become agent evidence. Plain Markdown imports
    without a manifest retain their documented recursive directory behavior.
    """
    manifest = root / "manifest.jsonl"
    if not manifest.exists() and not manifest.is_symlink():
        return sorted(root.rglob("*.md"), key=lambda p: p.relative_to(root).as_posix())
    if manifest.is_symlink() or not manifest.is_file():
        raise ValueError("Crawler manifest must be a regular local file")
    paths, seen = [], set()
    for line_number, line in enumerate(manifest.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid crawler manifest JSON on line {line_number}") from exc
        relative = row.get("path") if isinstance(row, dict) else None
        if not isinstance(relative, str) or not relative or "\\" in relative:
            raise ValueError(f"Invalid relative Markdown manifest path on line {line_number}")
        part_path = Path(relative)
        # Check both path dialects even when running on POSIX. Windows drive-only
        # paths and alternate data streams are not portable relative source paths.
        if (part_path.anchor or PureWindowsPath(relative).drive or ":" in relative
                or ".." in part_path.parts or part_path.suffix.lower() != ".md"):
            raise ValueError(f"Invalid relative Markdown manifest path on line {line_number}")
        path = root / part_path
        cursor = root
        for part in part_path.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError(f"Manifest source symlinks are not accepted: {relative}")
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(f"Manifest source path leaves raw directory: {relative}")
        if resolved in seen:
            raise ValueError(f"Duplicate Markdown path in crawler manifest: {relative}")
        if not path.is_file():
            raise ValueError(f"Manifest Markdown source is missing: {relative}")
        seen.add(resolved)
        paths.append(path)
    return sorted(paths, key=lambda p: p.relative_to(root).as_posix())


def read_markdown_sources(raw_dir: str | Path) -> list[dict]:
    root = Path(raw_dir).resolve()
    if not root.is_dir():
        raise ValueError(f"Raw Markdown directory does not exist: {root}")
    result = []
    for path in _import_paths(root):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError(f"Source symlinks/outside paths are not accepted: {path}")
        data = path.read_bytes()
        content = data.decode("utf-8-sig")
        metadata = {}
        if content.startswith("---\n") or content.startswith("---\r\n"):
            match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", content, re.S)
            if match:
                try:
                    parsed = yaml.safe_load(match.group(1))
                    if isinstance(parsed, dict):
                        metadata = {str(k): str(v) for k, v in parsed.items() if v is not None}
                except yaml.YAMLError:
                    pass  # Preserve malformed source verbatim; metadata is only convenience.
        heading = re.search(r"(?m)^#\s+(.+)$", content)
        title = metadata.get("title") or (heading.group(1).strip() if heading else path.stem)
        source_url = metadata.get("url") or metadata.get("source_url")
        date = metadata.get("created_at") or metadata.get("date") or metadata.get("published_at")
        if not source_url:
            match = re.search(r"(?im)^\s*(?:url|原文链接|来源链接|链接)\s*[:：]\s*(https?://\S+)", content)
            source_url = match.group(1) if match else None
        if not date:
            match = re.search(r"(?im)^\s*(?:date|发布时间|发布于|时间)\s*[:：]\s*(.+)$", content)
            date = match.group(1).strip() if match else None
        result.append({
            "doc_id": f"doc-{len(result) + 1:04d}",
            "relative_path": path.relative_to(root).as_posix(),
            "title": title,
            "url": source_url,
            "date": date,
            "platform_doc_id": (f"{metadata.get('source', 'zhihu')}:{metadata.get('kind', 'document')}:{metadata['id']}" if metadata.get("id") else None),
            "metadata": metadata,
            "sha256": sha256(data),
            "text": content,
            "bytes": data,
        })
    if not result:
        raise ValueError("No Markdown source documents found")
    return result


def fingerprint(documents: list[dict]) -> str:
    return sha256(json.dumps(
        [{"path": d["relative_path"], "sha256": d["sha256"]} for d in documents],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8"))


def estimate_tokens(text: str) -> int:
    """Conservative offline budget: CJK/non-ASCII one token, ASCII four chars.

    This is a planning estimate, not a claim about a provider tokenizer.
    All characters survive source splitting, including whitespace and the tail.
    """
    non_ascii = sum(ord(c) > 127 for c in text)
    return max(1, non_ascii + (len(text) - non_ascii + 3) // 4)


def date_sort_key(value: str | None) -> tuple:
    """Chronological ISO date order, honoring offsets; unparsed dates sort last."""
    if value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return (False, parsed.timestamp(), value)
        except (ValueError, OverflowError, OSError):
            pass
    return (True, 0, value or "")


def split_source(text: str, max_tokens: int) -> list[str]:
    if max_tokens < 1:
        raise ValueError("max_tokens must be positive")
    if not text:
        return [""]
    pieces = []
    remaining = text
    while estimate_tokens(remaining) > max_tokens:
        low, high = 1, len(remaining)
        while low < high:
            middle = (low + high + 1) // 2
            if estimate_tokens(remaining[:middle]) <= max_tokens:
                low = middle
            else:
                high = middle - 1
        end = low
        newline = remaining.rfind("\n", 0, end)
        if newline >= end // 2:
            end = newline + 1
        pieces.append(remaining[:end])
        remaining = remaining[end:]
    pieces.append(remaining)
    assert "".join(pieces) == text
    return pieces
