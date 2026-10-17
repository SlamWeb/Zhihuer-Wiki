"""Portable v2 checks exercise source evidence and actual files, without models."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from zhihuer_wiki.folder import render_wiki, validate_wiki


SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "wiki-v2"


@pytest.fixture
def wiki(tmp_path):
    target = tmp_path / "wiki"
    shutil.copytree(SAMPLE, target)
    return target


def read(wiki, name):
    return json.loads((wiki / name).read_text(encoding="utf-8"))


def write(wiki, name, data):
    (wiki / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def errors(wiki):
    report = validate_wiki(wiki)
    assert report["valid"] is False
    return "\n".join(report["errors"])


def test_checked_in_sample_and_render_are_deterministic(wiki):
    before = {p.relative_to(wiki): p.read_bytes() for p in wiki.rglob("*") if p.is_file()}
    report = render_wiki(wiki)
    assert report == {"valid": True, "errors": [], "warnings": [], "counts": {"topics": 1, "pages": 2, "entries": 6, "sources": 2}}
    assert before == {p.relative_to(wiki): p.read_bytes() for p in wiki.rglob("*") if p.is_file()}
    assert not (wiki / "entries").exists()


def test_pages_contain_complete_evidence_cards(wiki):
    entries, sources = read(wiki, "entries.json"), read(wiki, "sources.json")
    index = read(wiki, "index.json")
    for page in index["topics"][0]["pages"]:
        text = (wiki / "pages" / (page["id"] + ".md")).read_text(encoding="utf-8")
        for entry in entries:
            if entry["entry_id"] not in page["entry_ids"]:
                continue
            source = sources[entry["doc_id"]]
            for value in (entry["entry_id"], entry["doc_id"], entry["published_at"], entry["quote"], entry["paraphrase"], entry["context"], source["title"], "../" + source["path"]):
                assert value in text


def test_quote_must_be_contiguous_exact_source_text(wiki):
    entries = read(wiki, "entries.json")
    entries[0]["quote"] = "On our imaginary island, ... writing their planting month."
    write(wiki, "entries.json", entries)
    assert "exact continuous substring" in errors(wiki)
    before = (wiki / "index.md").read_bytes()
    with pytest.raises(ValueError, match="exact continuous substring"):
        render_wiki(wiki)
    assert (wiki / "index.md").read_bytes() == before
    assert not list(wiki.glob(".wiki-render-*"))


def test_source_hash_checks_exact_bytes_including_newlines(wiki):
    sources = read(wiki, "sources.json")
    path = wiki / next(iter(sources.values()))["path"]
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    assert "text_sha256 differs" in errors(wiki)


def test_duplicate_json_keys_cannot_silently_replace_registry_metadata(wiki):
    (wiki / "sources.json").write_text('{"same-doc": {}, "same-doc": {}}', encoding="utf-8")
    assert "duplicate JSON object key" in errors(wiki)


@pytest.mark.parametrize("value", ["2026-01-01", None, "not-a-date"])
def test_entry_date_cannot_drift_from_registry(wiki, value):
    entries = read(wiki, "entries.json")
    entries[0]["published_at"] = value
    write(wiki, "entries.json", entries)
    assert "published_at" in errors(wiki)


def test_unknown_dates_are_explicit_and_allowed_when_matching(wiki):
    entries, sources = read(wiki, "entries.json"), read(wiki, "sources.json")
    doc_id = entries[0]["doc_id"]
    sources[doc_id]["published_at"] = None
    for entry in entries:
        if entry["doc_id"] == doc_id:
            entry["published_at"] = None
    write(wiki, "entries.json", entries)
    write(wiki, "sources.json", sources)
    assert render_wiki(wiki)["valid"]
    assert "Unknown (null)" in (wiki / "pages/first-season.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("operation", ["missing", "duplicate", "unknown"])
def test_primary_coverage_is_complete_and_unique(wiki, operation):
    index = read(wiki, "index.json")
    pages = index["topics"][0]["pages"]
    if operation == "missing":
        pages[0]["entry_ids"].pop()
    elif operation == "duplicate":
        pages[1]["entry_ids"].append(pages[0]["entry_ids"][0])
    else:
        pages[0]["entry_ids"].append("nonexistent")
    write(wiki, "index.json", index)
    assert ("exactly one primary page" if operation != "unknown" else "unknown entry ID") in errors(wiki)


def test_duplicate_entry_id_is_rejected(wiki):
    entries = read(wiki, "entries.json")
    entries.append(entries[0].copy())
    write(wiki, "entries.json", entries)
    assert "duplicate entry ID" in errors(wiki)


def test_logical_document_and_entry_ids_preserve_original_colons_and_case(wiki):
    sources, entries, index = read(wiki, "sources.json"), read(wiki, "entries.json"), read(wiki, "index.json")
    original = entries[0]["doc_id"]
    logical_doc = "zhihu:answer:123"
    source = sources.pop(original)
    source["doc_id"] = logical_doc
    sources[logical_doc] = source
    for entry in entries:
        if entry["doc_id"] == original:
            entry["doc_id"] = logical_doc
    original_entry = entries[0]["entry_id"]
    entries[0]["entry_id"] = "zhihu:answer:123:Entry:1"
    index["topics"][0]["pages"][0]["entry_ids"][0] = entries[0]["entry_id"]
    entries[1]["entry_id"] = "zhihu:answer:123:entry:1"
    index["topics"][0]["pages"][0]["entry_ids"][1] = entries[1]["entry_id"]
    write(wiki, "sources.json", sources)
    write(wiki, "entries.json", entries)
    write(wiki, "index.json", index)
    assert render_wiki(wiki)["valid"]
    text = (wiki / "pages/first-season.md").read_text(encoding="utf-8")
    assert logical_doc in text and "zhihu:answer:123:Entry:1" in text
    assert "zhihu:answer:123:entry:1" in text
    assert "../sources/fictional-notebook-1.md" in text
    assert original_entry not in text


@pytest.mark.parametrize("field", ["quote", "paraphrase"])
def test_whitespace_only_evidence_cannot_satisfy_entry_fields(wiki, field):
    entries = read(wiki, "entries.json")
    entries[0][field] = " \n "
    write(wiki, "entries.json", entries)
    assert f"invalid {field}" in errors(wiki)


def test_brackets_and_backslashes_are_escaped_in_markdown_link_labels(wiki):
    index, sources = read(wiki, "index.json"), read(wiki, "sources.json")
    index["topics"][0]["title"] = r"Topic [one] \ notes"
    index["topics"][0]["pages"][0]["title"] = r"Page [one] \ notes"
    next(iter(sources.values()))["title"] = r"Source [one] \ notes"
    write(wiki, "index.json", index)
    write(wiki, "sources.json", sources)
    assert render_wiki(wiki)["valid"]
    text = (wiki / "pages/first-season.md").read_text(encoding="utf-8")
    assert r"[Source \[one\] \\ notes](../sources/fictional-notebook-1.md)" in text
    assert r"[Topic: Topic \[one\] \\ notes](../topics/seed-library.md)" in text


@pytest.mark.parametrize("bad_id", ["CON", "aux", "../escape", "a/b", "Chinese-中文", "seed-library", "FIRST-SEASON"])
def test_navigation_ids_are_globally_unique_safe_and_portable(wiki, bad_id):
    index = read(wiki, "index.json")
    index["topics"][0]["pages"][1]["id"] = bad_id
    write(wiki, "index.json", index)
    assert "navigation ID" in errors(wiki)


@pytest.mark.parametrize("bad_path", ["../outside.md", "sources/../outside.md", "sources/sub/file.md", "C:/outside.md", "sources\\outside.md", "sources/CON.md"])
def test_source_paths_cannot_escape_or_use_reserved_names(wiki, bad_path):
    sources = read(wiki, "sources.json")
    next(iter(sources.values()))["path"] = bad_path
    write(wiki, "sources.json", sources)
    assert "safe filename" in errors(wiki)


def test_case_insensitive_source_collision(wiki):
    sources = read(wiki, "sources.json")
    first, second = sources.values()
    second["path"] = first["path"].upper().replace("SOURCES/", "sources/").replace(".MD", ".md")
    write(wiki, "sources.json", sources)
    assert "source path collision" in errors(wiki)


def test_unreferenced_source_retained_as_provenance(wiki):
    sources = read(wiki, "sources.json")
    raw = b"Fictional unused source retained for provenance.\n"
    (wiki / "sources/unused.md").write_bytes(raw)
    sources["unused"] = {"doc_id": "unused", "title": "Fictional unused source", "published_at": None, "url": "", "path": "sources/unused.md", "text_sha256": hashlib.sha256(raw).hexdigest()}
    write(wiki, "sources.json", sources)
    report = render_wiki(wiki)
    assert report["valid"] and "zero entries" in "\n".join(report["warnings"])
    assert (wiki / "sources/unused.md").read_bytes() == raw


def test_small_page_warns_without_hard_limits(wiki):
    index = read(wiki, "index.json")
    pages = index["topics"][0]["pages"]
    pages[1]["entry_ids"].append(pages[0]["entry_ids"].pop())
    write(wiki, "index.json", index)
    report = render_wiki(wiki)
    assert report["valid"] and "small page" in "\n".join(report["warnings"])


def test_large_page_warns_without_rejecting_coverage(wiki):
    entries = read(wiki, "entries.json")
    index = read(wiki, "index.json")
    for number in range(31):
        entry = entries[0].copy()
        entry["entry_id"] = f"repeated-evidence-{number}"
        entries.append(entry)
        index["topics"][0]["pages"][0]["entry_ids"].append(entry["entry_id"])
    write(wiki, "entries.json", entries)
    write(wiki, "index.json", index)
    report = render_wiki(wiki)
    assert report["valid"] and "large page" in "\n".join(report["warnings"])


def test_large_chinese_context_warns_even_with_few_cards(wiki):
    entries = read(wiki, "entries.json")
    entries[0]["context"] = "这是明确标注的虚构上下文。" * 2500
    write(wiki, "entries.json", entries)
    report = render_wiki(wiki)
    assert report["valid"] and "large page" in "\n".join(report["warnings"])


def test_render_drift_and_stale_pages_are_reported_without_deletion(wiki):
    (wiki / "pages/first-season.md").write_text("incomplete summary", encoding="utf-8")
    assert "rendered content differs" in errors(wiki)
    stale = wiki / "pages/old-page.md"
    stale.write_text("Preserve until manually reviewed", encoding="utf-8")
    report = render_wiki(wiki)
    assert not report["valid"] and "stale unindexed" in "\n".join(report["errors"])
    assert stale.read_text(encoding="utf-8") == "Preserve until manually reviewed"
    assert "Exact quotation" in (wiki / "pages/first-season.md").read_text(encoding="utf-8")


def test_unsafe_output_is_rejected_before_other_outputs_change(wiki):
    (wiki / "pages/first-season.md").unlink()
    (wiki / "pages/first-season.md").mkdir()
    (wiki / "index.md").write_text("keep this until all output paths are safe", encoding="utf-8")
    with pytest.raises(ValueError, match="regular file"):
        render_wiki(wiki)
    assert (wiki / "index.md").read_text(encoding="utf-8") == "keep this until all output paths are safe"


def test_source_symlink_is_rejected_even_inside_root(wiki):
    source = wiki / "sources/fictional-notebook-1.md"
    target = wiki / "original.md"
    source.rename(target)
    try:
        source.symlink_to(target)
    except OSError:
        pytest.skip("host does not permit symlink creation")
    assert "symlink is not allowed" in errors(wiki)


def test_helper_runs_without_installation_or_site_packages(wiki):
    helper = SAMPLE.parents[1] / "scripts/wiki_folder.py"
    completed = subprocess.run([sys.executable, "-S", str(helper), "validate", "--wiki", str(wiki)], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert json.loads(completed.stdout)["valid"] is True
