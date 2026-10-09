"""Portable Markdown view, built only from a completely accepted pipeline."""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from .sources import date_sort_key, sha256


def _label(value: str) -> str:
    return str(value).replace("\n", " ").replace("\r", " ").replace("[", "\\[").replace("]", "\\]")


def _entry_text(entry: dict, *, prefix: str = "../") -> str:
    date = entry["source_date"] or "日期未记录"
    lines = [f"# {_label(entry['entry_id'])}", "", entry["paraphrase"], "",
             f"来源：[原文 {entry['doc_id']}]({prefix}sources/{entry['doc_id']}.md)",
             f"同源条目：[相关条目]({prefix}sources/{entry['doc_id']}-links.md)",
             f"原文日期：{date}", f"表达时间：{entry['time'] or '未记录'}", "",
             f"语境与条件：{entry['conditions']}", f"局部主题提示：{entry['topic_hint']}", "", "## 原文证据", ""]
    for quote in entry["quotes"]:
        lines.extend(["> " + quote.replace("\n", "\n> "), ""])
    return "\n".join(lines).rstrip() + "\n"


def publish(workspace: Path, *, author: str, documents: list[dict], entries: list[dict],
            subpages: list[dict], topics: list[dict], statements: list[dict], grouping: dict) -> dict:
    if not entries:
        raise ValueError("A corpus with zero canonical entries cannot publish a Wiki")
    staging = workspace / ".work" / "wiki-staging"
    # Only our known temporary directory is recreated; final output is never overwritten.
    if staging.exists():
        if staging.is_symlink() or not staging.resolve().is_relative_to((workspace / ".work").resolve()):
            raise ValueError("Unsafe Wiki staging path")
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    for folder in ("topics", "subpages", "entries", "sources"):
        (staging / folder).mkdir()
    by_entry = {entry["entry_id"]: entry for entry in entries}
    by_subpage = {page["subpage_id"]: page for page in subpages}
    links = []

    def write(relative: str, content: str, targets: list[str] | None = None) -> None:
        (staging / relative).write_text(content, encoding="utf-8", newline="\n")
        for target in targets or []:
            links.append({"from": relative, "to": target})

    coverage = {d["doc_id"]: 0 for d in documents}
    def chronological(ids):
        return sorted(ids, key=lambda e: (date_sort_key(by_entry[e]["source_date"]), e))

    for entry in entries:
        coverage[entry["doc_id"]] += 1
        write(f"entries/{entry['entry_id']}.md", _entry_text(entry), [f"../sources/{entry['doc_id']}.md", f"../sources/{entry['doc_id']}-links.md"])
    for document in documents:
        source = workspace / "evidence" / "sources" / f"{document['doc_id']}.md"
        (staging / "sources" / f"{document['doc_id']}.md").write_bytes(source.read_bytes())
        lines = [f"# {_label(document['title'])}：同源条目", "", f"[归档原文]({document['doc_id']}.md)", "",
                 f"平台文档 ID：{document.get('platform_doc_id') or '未记录'}", f"原始文件：{document['relative_path']}",
                 f"原文日期：{document['date'] or '未记录'}", ""]
        if document["url"]:
            lines.extend([f"原始链接：{document['url']}", ""])
        supported = chronological([e["entry_id"] for e in entries if e["doc_id"] == document["doc_id"]])
        lines.extend(f"- [{e}](../entries/{e}.md)：{_label(by_entry[e]['paraphrase'])}" for e in supported)
        if not supported:
            lines.append("原文已明确覆盖，本次抽取为零条目。")
        write(f"sources/{document['doc_id']}-links.md", "\n".join(lines) + "\n",
              [f"{document['doc_id']}.md", *[f"../entries/{e}.md" for e in supported]])

    source_index = ["# 原文目录", "", "所有原文均保留；条目为零的原文也列入覆盖记录。", ""]
    for document in documents:
        source_index.append(f"- [{_label(document['title'])}]({document['doc_id']}.md) / [同源条目]({document['doc_id']}-links.md) — {document['date'] or '日期未记录'}；{coverage[document['doc_id']]} 条")
    write("sources/index.md", "\n".join(source_index) + "\n", [target for d in documents for target in (f"{d['doc_id']}.md", f"{d['doc_id']}-links.md")])

    for page in subpages:
        dates = sorted([by_entry[e]["source_date"] for e in page["entry_ids"] if by_entry[e]["source_date"]], key=date_sort_key)
        lines = [f"# {_label(page['title'])}", "", page["description"], "",
                 f"主要条目：{len(page['entry_ids'])}；日期范围：{dates[0] + ' — ' + dates[-1] if dates else '未记录'}", "", "## 主要条目", ""]
        targets = []
        for entry_id in chronological(page["entry_ids"]):
            lines.append(f"- [{_label(entry_id)}](../entries/{entry_id}.md) — {by_entry[entry_id]['source_date'] or '日期未记录'}：{_label(by_entry[entry_id]['paraphrase'])}")
            targets.append(f"../entries/{entry_id}.md")
        if page["secondary_entry_ids"]:
            lines.extend(["", "## 相关条目引用", ""])
            for entry_id in chronological(page["secondary_entry_ids"]):
                lines.append(f"- [{entry_id}](../entries/{entry_id}.md)")
                targets.append(f"../entries/{entry_id}.md")
        write(f"subpages/{page['subpage_id']}.md", "\n".join(lines) + "\n", targets)

    for topic in topics:
        lines = [f"# {_label(topic['title'])}", "", topic["description"], "", "## 子页", ""]
        for subpage_id in topic["subpage_ids"]:
            page = by_subpage[subpage_id]
            lines.append(f"- [{_label(page['title'])}](../subpages/{subpage_id}.md) — {len(page['entry_ids'])} 条；{_label(page['description'])}")
        write(f"topics/{topic['topic_id']}.md", "\n".join(lines) + "\n", [f"../subpages/{s}.md" for s in topic["subpage_ids"]])

    overview_lines = [f"# {_label(author)}：来源绑定概览", "", "每个陈述保留其条目引用；完整表达、条件与原文在条目页中。", ""]
    overview_targets = []
    for statement in statements:
        refs = "、".join(f"[{e}](entries/{e}.md)" for e in statement["entry_ids"])
        overview_lines.extend([statement["text"], "", f"依据：{refs}", ""])
        overview_targets.extend(f"entries/{e}.md" for e in statement["entry_ids"])
    if not statements:
        overview_lines.append("本批材料未生成可负责的概览陈述；请通过主题目录阅读原始条目。")
    write("overview.md", "\n".join(overview_lines) + "\n", overview_targets)

    index_lines = [f"# {_label(author)} Wiki", "", f"{len(documents)} 篇原文；{len(entries)} 条规范条目；{len(subpages)} 个完整子页。", "",
                   "[来源绑定概览](overview.md) / [连续阅读](readable.md) / [原文目录](sources/index.md)", "", "## 主题", ""]
    for topic in topics:
        index_lines.append(f"- [{_label(topic['title'])}](topics/{topic['topic_id']}.md)：{_label(topic['description'])}")
    write("index.md", "\n".join(index_lines) + "\n", ["overview.md", "readable.md", "sources/index.md", *[f"topics/{t['topic_id']}.md" for t in topics]])

    readable = [f"# {_label(author)}：连续阅读", "", "[主题目录](index.md)", ""]
    readable_targets = ["index.md"]
    for topic in topics:
        readable.extend([f"## {_label(topic['title'])}", "", topic["description"], ""])
        for subpage_id in topic["subpage_ids"]:
            page = by_subpage[subpage_id]
            readable.extend([f"### {_label(page['title'])}", "", page["description"], ""])
            for entry_id in chronological(page["entry_ids"]):
                entry = by_entry[entry_id]
                text = _entry_text(entry, prefix="").replace("# " + _label(entry_id), "#### " + _label(entry_id), 1).replace("## 原文证据", "##### 原文证据")
                readable.extend([text, ""])
                readable_targets.append(f"sources/{entry['doc_id']}.md")
                readable_targets.append(f"sources/{entry['doc_id']}-links.md")
    write("readable.md", "\n".join(readable).rstrip() + "\n", readable_targets)
    manifest = {
        "version": 1, "author": author, "documents": [{**d, "entry_count": coverage[d["doc_id"]]} for d in documents],
        "entry_ids": list(by_entry), "subpage_ids": list(by_subpage), "topic_ids": [t["topic_id"] for t in topics],
        "grouping": grouping, "links": links,
        "files": {p.relative_to(staging).as_posix(): sha256(p.read_bytes()) for p in sorted(staging.rglob("*")) if p.is_file()},
    }
    (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    errors = validate_export(staging)
    if errors:
        raise ValueError("Export validation failed: " + "; ".join(errors))
    output = workspace / "wiki"
    if output.exists():
        # Resume the narrow crash window between atomic publication and state commit.
        if not validate_export(output) and json.loads((output / "manifest.json").read_text(encoding="utf-8")) == manifest:
            shutil.rmtree(staging)
            return manifest
        raise ValueError("Final Wiki already exists; refusing to overwrite evidence")
    os.replace(staging, output)
    return manifest


def validate_export(wiki: Path) -> list[str]:
    errors = []
    try:
        manifest = json.loads((wiki / "manifest.json").read_text(encoding="utf-8"))
        root = wiki.resolve()
        for relative, digest in manifest["files"].items():
            path = wiki / relative
            if Path(relative).is_absolute() or not path.resolve().is_relative_to(root):
                errors.append(f"Unsafe export path: {relative}")
            elif path.is_symlink() or not path.is_file() or sha256(path.read_bytes()) != digest:
                errors.append(f"Missing or changed export file: {relative}")
        for link in manifest["links"]:
            source = wiki / link["from"]
            target = source.parent / link["to"]
            if not source.resolve().is_relative_to(root) or not target.resolve().is_relative_to(root) or not target.is_file():
                errors.append(f"Broken/unsafe relative link: {link['from']} -> {link['to']}")
        # All program-owned navigation links must actually be present in the file.
        for link in manifest["links"]:
            source = wiki / link["from"]
            if source.is_file() and f"]({link['to']})" not in source.read_text(encoding="utf-8-sig"):
                errors.append(f"Missing Markdown link: {link['from']} -> {link['to']}")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        errors.append(f"Invalid export manifest: {exc}")
    return errors
