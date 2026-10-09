"""Render crawler items into auditable Markdown files."""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, NavigableString, Tag

from zhihuer_wiki.vendor.crawler.models import ContentItem, CreatorProfile
from zhihuer_wiki.vendor.crawler.storage import write_jsonl

MAX_FILENAME_STEM = 90


def html_to_markdown(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()

    root = soup.body or soup
    markdown = _render_children(root)
    return _normalize_markdown(markdown)


def render_item_markdown(item: ContentItem) -> str:
    body = html_to_markdown(item.content_html) or item.content_text
    front_matter = _front_matter(
        {
            "source": item.source,
            "kind": item.kind,
            "id": item.id,
            "title": item.title,
            "url": item.url,
            "author_token": item.author_token,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
            "fetched_at": item.fetched_at,
            **item.metadata,
        }
    )
    title = item.title.strip() or f"{item.kind} {item.id}"
    return f"{front_matter}\n\n# {title}\n\n{body.strip()}\n"


def write_markdown_corpus(
    items: Iterable[ContentItem],
    directory: Path,
    *,
    write_manifest: bool = True,
    overwrite: bool = True,
    group_by_kind: bool = True,
) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    manifest_rows: list[dict[str, Any]] = []
    materialized = list(items)

    for item in materialized:
        item_dir = directory / item.kind if group_by_kind else directory
        item_dir.mkdir(parents=True, exist_ok=True)
        path = item_dir / item_filename(item)
        if path.exists() and not overwrite:
            raise FileExistsError(f"Refusing to overwrite existing markdown file: {path}")
        path.write_text(render_item_markdown(item), encoding="utf-8", newline="\n")
        paths.append(path)
        row = item.to_dict()
        row["path"] = path.relative_to(directory).as_posix()
        manifest_rows.append(row)

    if write_manifest:
        write_jsonl(manifest_rows, directory / "manifest.jsonl", append=False)

    return paths


def write_profile(profile: CreatorProfile, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "profile.json"
    path.write_text(profile.to_json() + "\n", encoding="utf-8", newline="\n")
    return path


def item_filename(item: ContentItem) -> str:
    stem = slugify_filename(f"{item.kind}-{item.id}-{item.title}")
    return f"{stem[:MAX_FILENAME_STEM].rstrip('-')}.md"


def slugify_filename(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", value)
    cleaned = re.sub(r"\s+", "-", cleaned.strip())
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    return cleaned.strip(".-") or "untitled"


def _front_matter(values: dict[str, Any]) -> str:
    lines = ["---"]
    for key, value in values.items():
        if value in (None, "", [], {}):
            continue
        lines.append(f"{key}: {_yaml_scalar(value)}")
    lines.append("---")
    return "\n".join(lines)


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{text}"'


def _render_children(tag: Tag) -> str:
    return "".join(_render_node(child) for child in tag.children)


def _render_node(node: Tag | NavigableString) -> str:
    if isinstance(node, NavigableString):
        return str(node)
    if not isinstance(node, Tag):
        return ""

    name = node.name.lower()
    if name in {"p", "div", "section", "article"}:
        return f"\n\n{_render_children(node).strip()}\n\n"
    if name == "br":
        return "\n"
    if name in {"strong", "b"}:
        return f"**{_render_children(node).strip()}**"
    if name in {"em", "i"}:
        return f"*{_render_children(node).strip()}*"
    if name == "code":
        text = node.get_text("", strip=True)
        return f"`{text}`"
    if name == "pre":
        text = node.get_text("\n").strip()
        return f"\n\n```\n{text}\n```\n\n"
    if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        level = int(name[1])
        return f"\n\n{'#' * level} {_render_children(node).strip()}\n\n"
    if name == "blockquote":
        body = _normalize_markdown(_render_children(node))
        quoted = "\n".join(f"> {line}" if line else ">" for line in body.splitlines())
        return f"\n\n{quoted}\n\n"
    if name in {"ul", "ol"}:
        ordered = name == "ol"
        lines: list[str] = []
        index = 1
        for child in node.find_all("li", recursive=False):
            prefix = f"{index}. " if ordered else "- "
            lines.append(prefix + _normalize_markdown(_render_children(child)).replace("\n", "\n  "))
            index += 1
        return "\n\n" + "\n".join(lines) + "\n\n"
    if name == "a":
        text = _render_children(node).strip() or node.get("href", "")
        href = node.get("href", "")
        return f"[{text}]({href})" if href else text
    if name == "img":
        src = node.get("src") or node.get("data-original") or node.get("data-actualsrc") or ""
        alt = node.get("alt") or "image"
        return f"![{alt}]({src})" if src else ""

    return _render_children(node)


def _normalize_markdown(value: str) -> str:
    lines = [line.rstrip() for line in value.splitlines()]
    text = "\n".join(lines)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
