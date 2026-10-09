"""File-job compiler with immutable evidence and completely checked stage gates.

Semantic responses come from a user's coding agent, never an LLM HTTP API.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator

from . import export
from .grouping import group_entries
from .sources import date_sort_key, estimate_tokens, fingerprint, read_markdown_sources, sha256, split_source


class PipelineError(ValueError):
    """A rejected response or incomplete pipeline; accepted evidence is retained."""


DEFAULTS = {
    "extraction_batch_docs": 10, "extraction_max_tokens": 20000,
    "classification_batch_entries": 100, "classification_max_tokens": 20000,
    "overview_max_tokens": 20000, "directory_max_tokens": 20000,
    "grouping_backend": "embedding", "grouping_threshold": 200,
    "embedding_model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    "candidate_groups": None,
}


def _json(data) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".writing-", delete=False) as file:
        temp = Path(file.name)
        try:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _state_path(root: Path) -> Path:
    return root / ".work" / "state.json"


def _load(root: Path) -> dict:
    try:
        state = _read(_state_path(root))
    except (OSError, ValueError) as exc:
        raise PipelineError(f"No valid workspace state at {_state_path(root)}") from exc
    if state.get("version") != 1:
        raise PipelineError("Unsupported workspace version")
    return state


def _save(root: Path, state: dict) -> None:
    _atomic(_state_path(root), _json(state))


@contextmanager
def _lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    lock = root / ".pipeline.lock"
    # Persistent inode/file with OS-owned locking. The kernel releases the lock
    # on hard process exit, so stale legacy PID files cannot block recovery.
    handle = lock.open("a+b")
    if os.name == "nt":
        import msvcrt
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        acquire = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        release = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        acquire = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        release = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    try:
        acquire()
    except OSError as exc:
        handle.close()
        raise PipelineError(f"Workspace is busy: {lock}") from exc
    try:
        yield
    finally:
        try:
            handle.seek(0)
            release()
        finally:
            handle.close()


def _object(properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _array(items: dict, minimum: int = 0) -> dict:
    return {"type": "array", "items": items, "minItems": minimum}


def _string() -> dict:
    return {"type": "string", "minLength": 1}


def _id_array(ids: list[str], minimum: int = 0) -> dict:
    return _array({"type": "string", "enum": ids}, minimum)


def _schema(stage: str, job_id: str, payload: dict) -> dict:
    if stage == "extraction":
        entry = _object({
            "entry_id": {"type": "string", "pattern": "^" + re.escape(job_id) + r"-e[0-9]{4,}$"},
            "paraphrase": _string(), "quotes": _array(_string(), 1),
            "time": {"anyOf": [_string(), {"type": "null"}]},
            "conditions": {"type": "string"}, "topic_hint": _string(),
        })
        shape = _object({"documents": _array(_object({
            "part_id": {"type": "string", "enum": [d["part_id"] for d in payload["documents"]]},
            "doc_id": {"type": "string", "enum": list(dict.fromkeys(d["doc_id"] for d in payload["documents"]))},
            "entries": _array(entry),
        }), len(payload["documents"]))})
    elif stage == "classification":
        ids = [e["entry_id"] for e in payload["entries"]]
        shape = _object({"subpages": _array(_object({
            "subpage_id": {"type": "string", "pattern": "^" + re.escape(job_id) + r"-s[0-9]{3,}$"},
            "title": _string(), "description": _string(),
            "entry_ids": _id_array(ids, 1), "secondary_entry_ids": _id_array(ids),
        }), 1)})
    elif stage == "directory":
        shape = _object({"topics": _array(_object({
            "topic_id": {"type": "string", "pattern": r"^topic-[a-z0-9][a-z0-9-]{0,79}$"},
            "title": _string(), "description": _string(),
            "subpage_ids": _id_array([p["subpage_id"] for p in payload["subpages"]], 1),
        }), 1)})
    else:
        statements = _array(_object({
            "text": _string(), "entry_ids": _id_array([e["entry_id"] for e in payload["entries"]], 1),
        }))
        statements["maxItems"] = 3
        shape = _object({"statements": statements})
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "title": job_id, **shape}


def _job(root: Path, state: dict, stage: str, job_id: str, payload: dict) -> None:
    folder = root / ".work" / "jobs" / job_id
    input_data = _json(payload)
    schema_data = _json(_schema(stage, job_id, payload))
    instructions = (Path(__file__).parent / "prompts" / f"{stage}.md").read_text(encoding="utf-8")
    prompt = (instructions + "\n\n## Job files\n\n"
              + f"Job: {job_id}\nInput: {(folder / 'input.json').resolve()}\n"
              + f"Schema: {(folder / 'schema.json').resolve()}\n"
              + f"Save response JSON to: {(folder / 'response.json').resolve()}\n"
              + "\nRead the full input file before responding. Never replace it with a sample.\n").encode("utf-8")
    _atomic(folder / "input.json", input_data)
    _atomic(folder / "schema.json", schema_data)
    _atomic(folder / "prompt.md", prompt)
    state["jobs"][job_id] = {
        "stage": stage, "status": "pending", "input_sha256": sha256(input_data),
        "schema_sha256": sha256(schema_data), "prompt_sha256": sha256(prompt), "response_sha256": None,
    }


def _batches(items: list[dict], count: int, tokens: int, text_key=None) -> list[list[dict]]:
    batches, current, size = [], [], 0
    for item in items:
        cost = estimate_tokens(item[text_key] if text_key else _json(item).decode("utf-8"))
        if not text_key and cost > tokens:
            raise PipelineError(f"Canonical entry {item.get('entry_id')} exceeds the {tokens} estimated-token budget; evidence retained intact, increase budget in a new workspace")
        if current and (len(current) >= count or size + cost > tokens):
            batches.append(current)
            current, size = [], 0
        current.append(item)
        size += cost
    if current:
        batches.append(current)
    return batches


def _response(root: Path, job_id: str) -> dict:
    return _read(root / "evidence" / "responses" / f"{job_id}.json")


def _canonical_entries(root: Path, state: dict) -> list[dict]:
    documents = {d["doc_id"]: d for d in state["documents"]}
    result = []
    for job_id, job in state["jobs"].items():
        if job["stage"] != "extraction" or job["status"] != "accepted":
            continue
        for part in _response(root, job_id)["documents"]:
            document = documents[part["doc_id"]]
            for entry in part["entries"]:
                result.append({**entry, "doc_id": part["doc_id"], "part_id": part["part_id"],
                               "source_date": document["date"], "source_url": document["url"],
                               "platform_doc_id": document.get("platform_doc_id"),
                               "original_source_path": document["relative_path"], "extraction_job_id": job_id})
    return result


def _collected(root: Path, state: dict, stage: str, key: str) -> list[dict]:
    return [value for job_id, job in state["jobs"].items()
            if job["stage"] == stage and job["status"] == "accepted"
            for value in _response(root, job_id)[key]]


def _validate_response(root: Path, state: dict, job_id: str, value: dict) -> None:
    job = state["jobs"][job_id]
    folder = root / ".work" / "jobs" / job_id
    for filename, key in (("input.json", "input_sha256"), ("schema.json", "schema_sha256"), ("prompt.md", "prompt_sha256")):
        if sha256((folder / filename).read_bytes()) != job[key]:
            raise PipelineError(f"Job file changed: {job_id}/{filename}")
    errors = sorted(Draft202012Validator(_read(folder / "schema.json")).iter_errors(value), key=lambda e: str(list(e.path)))
    if errors:
        first = errors[0]
        raise PipelineError(f"{job_id}: schema error at {list(first.path)}: {first.message}")
    payload = _read(folder / "input.json")
    stage = job["stage"]

    def exactly(actual: list[str], expected: list[str], label: str) -> None:
        if len(actual) != len(set(actual)):
            raise PipelineError(f"{job_id}: duplicate {label} (primary assignment must occur exactly once)")
        if set(actual) != set(expected):
            raise PipelineError(f"{job_id}: missing or unexpected {label}; expected complete coverage")

    if stage == "extraction":
        parts = {p["part_id"]: p for p in payload["documents"]}
        exactly([p["part_id"] for p in value["documents"]], list(parts), "source part IDs")
        ids = []
        for part in value["documents"]:
            source = parts[part["part_id"]]
            if part["doc_id"] != source["doc_id"]:
                raise PipelineError(f"{job_id}: source document ID does not match part")
            for entry in part["entries"]:
                ids.append(entry["entry_id"])
                if not entry["paraphrase"].strip() or not entry["topic_hint"].strip():
                    raise PipelineError(f"{job_id}: empty semantic entry text")
                for quote in entry["quotes"]:
                    if not quote.strip() or quote not in source["text"]:
                        raise PipelineError(f"{job_id}: invented/non-source exact quote for {entry['entry_id']}")
        exactly(ids, ids, "entry IDs")
    elif stage == "classification":
        pages = value["subpages"]
        exactly([p["subpage_id"] for p in pages], [p["subpage_id"] for p in pages], "subpage IDs")
        exactly([e for p in pages for e in p["entry_ids"]], [e["entry_id"] for e in payload["entries"]], "primary entry IDs")
        for page in pages:
            if len(page["secondary_entry_ids"]) != len(set(page["secondary_entry_ids"])):
                raise PipelineError(f"{job_id}: duplicate secondary entry references")
            if set(page["secondary_entry_ids"]) & set(page["entry_ids"]):
                raise PipelineError(f"{job_id}: primary entries repeated as secondary on same subpage")
    elif stage == "directory":
        topics = value["topics"]
        exactly([t["topic_id"] for t in topics], [t["topic_id"] for t in topics], "topic IDs")
        exactly([p for t in topics for p in t["subpage_ids"]], [p["subpage_id"] for p in payload["subpages"]], "subpage parent IDs")
    else:
        for statement in value["statements"]:
            if not statement["text"].strip() or len(statement["entry_ids"]) != len(set(statement["entry_ids"])):
                raise PipelineError(f"{job_id}: empty statement or duplicate overview evidence")


def _check_evidence(root: Path, state: dict) -> list[str]:
    errors = []
    for document in state["documents"]:
        path = root / "evidence" / "sources" / f"{document['doc_id']}.md"
        if path.is_symlink() or not path.is_file() or sha256(path.read_bytes()) != document["sha256"]:
            errors.append(f"Missing or changed source snapshot: {document['doc_id']}")
    for job_id, job in state["jobs"].items():
        if job["status"] != "accepted":
            continue
        try:
            response = _response(root, job_id)
            if sha256(_json(response)) != job["response_sha256"]:
                errors.append(f"Changed accepted response: {job_id}")
            _validate_response(root, state, job_id, response)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(str(exc))
    return errors


def _advance(root: Path, state: dict) -> None:
    while state["stage"] != "completed":
        stage = state["stage"]
        jobs = [job for job in state["jobs"].values() if job["stage"] == stage]
        if not jobs or any(job["status"] != "accepted" for job in jobs):
            return
        evidence_errors = _check_evidence(root, state)
        if evidence_errors:
            raise PipelineError("; ".join(evidence_errors))
        settings = state["settings"]
        entries = _canonical_entries(root, state)
        if stage == "extraction":
            _atomic(root / "evidence" / "entries.json", _json(entries))
            state["entry_count"] = len(entries)
            if not entries:
                raise PipelineError("All sources were covered but no canonical entries were extracted; no Wiki published")
            grouping = group_entries(entries, backend=settings["grouping_backend"], threshold=settings["grouping_threshold"],
                                     embedding_model=settings["embedding_model"], candidate_groups=settings["candidate_groups"])
            state["grouping"] = grouping["provenance"]
            _atomic(root / "derived" / "candidate-groups.json", _json(grouping))
            by_id = {e["entry_id"]: e for e in entries}
            index = 0
            for group in grouping["groups"]:
                assigned = [by_id[e] for e in group["entry_ids"]]
                for batch in _batches(assigned, settings["classification_batch_entries"], settings["classification_max_tokens"]):
                    index += 1
                    _job(root, state, "classification", f"classify-{index:04d}", {
                        "author": state["author"], "candidate_group_id": group["group_id"],
                        "candidate_backend": state["grouping"]["actual_backend"], "entries": batch,
                        "token_estimate": estimate_tokens(_json(batch).decode("utf-8")),
                        "oversized_single_entry": len(batch) == 1 and estimate_tokens(_json(batch).decode("utf-8")) > settings["classification_max_tokens"],
                    })
            state["stage"] = "classification"
        elif stage == "classification":
            pages = _collected(root, state, "classification", "subpages")
            primaries = [e for p in pages for e in p["entry_ids"]]
            if len(primaries) != len(set(primaries)) or set(primaries) != {e["entry_id"] for e in entries}:
                raise PipelineError("Global primary entry assignment is incomplete or duplicated")
            _atomic(root / "derived" / "subpages.json", _json(pages))
            by_id = {e["entry_id"]: e for e in entries}
            compact = []
            for page in pages:
                dates = sorted([by_id[e]["source_date"] for e in page["entry_ids"] if by_id[e]["source_date"]], key=date_sort_key)
                compact.append({"subpage_id": page["subpage_id"], "title": page["title"], "description": page["description"],
                                "entry_count": len(page["entry_ids"]), "time_range": [dates[0], dates[-1]] if dates else []})
            payload = {"author": state["author"], "subpages": compact}
            if estimate_tokens(_json(payload).decode("utf-8")) > settings["directory_max_tokens"]:
                raise PipelineError("Compact global directory exceeds directory_max_tokens; intact subpages retained, no truncation or Wiki publication")
            _job(root, state, "directory", "directory-0001", payload)
            state["stage"] = "directory"
        elif stage == "directory":
            topics = _collected(root, state, "directory", "topics")
            _atomic(root / "derived" / "topics.json", _json(topics))
            pages = _collected(root, state, "classification", "subpages")
            by_id = {e["entry_id"]: e for e in entries}
            by_page = {p["subpage_id"]: p for p in pages}
            index = 0
            for topic in topics:
                assigned = [by_id[e] for p in topic["subpage_ids"] for e in by_page[p]["entry_ids"]]
                for batch in _batches(assigned, settings["classification_batch_entries"], settings["overview_max_tokens"]):
                    index += 1
                    _job(root, state, "overview", f"overview-{index:04d}", {
                        "author": state["author"], "topic": {"topic_id": topic["topic_id"], "title": topic["title"], "description": topic["description"]},
                        "entries": batch, "scope": "independent evidence-bound topic batch; no global worldview inference",
                        "token_estimate": estimate_tokens(_json(batch).decode("utf-8")),
                    })
            state["stage"] = "overview"
        else:
            pages = _collected(root, state, "classification", "subpages")
            topics = _collected(root, state, "directory", "topics")
            statements = _collected(root, state, "overview", "statements")
            _atomic(root / "derived" / "overview.json", _json(statements))
            export.publish(root, author=state["author"], documents=state["documents"], entries=entries,
                           subpages=pages, topics=topics, statements=statements, grouping=state["grouping"])
            state["stage"] = "completed"
            state["completed_at"] = datetime.now(timezone.utc).isoformat()
        state["last_error"] = None
        _save(root, state)


def start(workspace, raw_dir, author, **settings) -> dict:
    """Snapshot local Markdown and prepare independent extraction file jobs."""
    root = Path(workspace).resolve()
    unknown = set(settings) - set(DEFAULTS)
    if unknown:
        raise PipelineError("Unknown settings: " + ", ".join(sorted(unknown)))
    config = {**DEFAULTS, **settings}
    for key in ("extraction_batch_docs", "extraction_max_tokens", "classification_batch_entries", "classification_max_tokens", "overview_max_tokens", "directory_max_tokens"):
        if isinstance(config[key], bool) or not isinstance(config[key], int) or config[key] < 1:
            raise PipelineError(f"{key} must be a positive integer")
    if not isinstance(config["grouping_threshold"], int) or config["grouping_threshold"] < 0:
        raise PipelineError("grouping_threshold must be nonnegative")
    if config["grouping_backend"] not in {"embedding", "tfidf", "none"}:
        raise PipelineError("grouping_backend must be embedding, tfidf, or none")
    if config["candidate_groups"] is not None and (not isinstance(config["candidate_groups"], int) or config["candidate_groups"] < 1):
        raise PipelineError("candidate_groups must be a positive integer or null")
    if not isinstance(author, str) or not author.strip():
        raise PipelineError("author must be a nonempty string")
    try:
        documents = read_markdown_sources(raw_dir)
    except (OSError, ValueError) as exc:
        raise PipelineError(str(exc)) from exc
    for document in documents:
        source_author = document["metadata"].get("author_token")
        if source_author and source_author != author:
            raise PipelineError(
                f"Source author_token {source_author!r} differs from requested author {author!r}: {document['relative_path']}"
            )
    with _lock(root):
        if _state_path(root).exists():
            state = _load(root)
            if state["source_fingerprint"] != fingerprint(documents) or state["settings"] != config or state["author"] != author:
                raise PipelineError("Sources/settings/author changed; create a new workspace to preserve accepted evidence")
            return _status(root, state)
        if (root / "wiki").exists() or (root / "evidence").exists() or (root / "derived").exists():
            raise PipelineError("Workspace contains pipeline artifacts without state; choose a new workspace")
        metadata = [{k: v for k, v in d.items() if k not in {"text", "bytes"}} for d in documents]
        state = {"version": 1, "author": author, "raw_dir": str(Path(raw_dir).resolve()), "settings": config,
                 "source_fingerprint": fingerprint(documents), "documents": metadata, "stage": "extraction",
                 "jobs": {}, "entry_count": 0, "grouping": None, "last_error": None, "failed_attempts": 0,
                 "created_at": datetime.now(timezone.utc).isoformat()}
        units = []
        for document in documents:
            _atomic(root / "evidence" / "sources" / f"{document['doc_id']}.md", document["bytes"])
            pieces = split_source(document["text"], config["extraction_max_tokens"])
            for i, text in enumerate(pieces):
                units.append({"part_id": f"{document['doc_id']}-part-{i + 1:04d}", "doc_id": document["doc_id"],
                              "part_number": i + 1, "part_count": len(pieces), "title": document["title"],
                              "date": document["date"], "url": document["url"],
                              "platform_doc_id": document.get("platform_doc_id"), "original_source_path": document["relative_path"], "text": text})
        for i, batch in enumerate(_batches(units, config["extraction_batch_docs"], config["extraction_max_tokens"], "text")):
            _job(root, state, "extraction", f"extract-{i + 1:04d}", {"author": author, "documents": batch,
                 "token_estimate": sum(estimate_tokens(p["text"]) for p in batch), "budget_method": "offline conservative character estimate"})
        _save(root, state)
        return _status(root, state)


def _status(root: Path, state: dict) -> dict:
    accepted = sum(j["status"] == "accepted" for j in state["jobs"].values())
    completed = state["stage"] == "completed"
    return {"workspace": str(root), "state": "completed" if completed else "failed" if state["last_error"] else "pending",
            "stage": state["stage"], "author": state["author"], "documents": len(state["documents"]),
            "entries": state["entry_count"], "jobs": {"total": len(state["jobs"]), "accepted": accepted, "pending": len(state["jobs"]) - accepted},
            "failed_attempts": state["failed_attempts"], "grouping": state["grouping"],
            "wiki_path": str(root / "wiki") if completed else None, "last_error": state["last_error"]}


def status(workspace) -> dict:
    root = Path(workspace).resolve()
    return _status(root, _load(root))


def next_jobs(workspace) -> list[dict]:
    root = Path(workspace).resolve()
    with _lock(root):
        state = _load(root)
        try:
            _advance(root, state)
        except (OSError, ValueError) as exc:
            state["last_error"] = str(exc)
            _save(root, state)
            raise PipelineError(str(exc)) from exc
        return [{"job_id": job_id, "stage": job["stage"],
                 **{key: str((root / ".work" / "jobs" / job_id / filename).resolve())
                    for key, filename in (("prompt_path", "prompt.md"), ("schema_path", "schema.json"), ("input_path", "input.json"), ("response_path", "response.json"))}}
                for job_id, job in state["jobs"].items() if job["status"] == "pending" and job["stage"] == state["stage"]]


def submit(workspace, job_id, response_path) -> dict:
    root = Path(workspace).resolve()
    with _lock(root):
        state = _load(root)
        if job_id not in state["jobs"]:
            raise PipelineError(f"Unknown job ID: {job_id}")
        try:
            raw_response = Path(response_path).read_bytes()
            value = json.loads(raw_response.decode("utf-8-sig"))
            _validate_response(root, state, job_id, value)
            digest = sha256(_json(value))
            job = state["jobs"][job_id]
            if job["status"] == "accepted":
                if digest != job["response_sha256"]:
                    raise PipelineError("Accepted response is immutable; changed response requires a new workspace")
                _advance(root, state)
                return _status(root, state)
            if job["stage"] != state["stage"]:
                raise PipelineError("Job is not ready in the current stage")
            evidence_errors = _check_evidence(root, state)
            if evidence_errors:
                raise PipelineError("; ".join(evidence_errors))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            state["failed_attempts"] += 1
            attempt = {"job_id": job_id, "error": str(exc), "at": datetime.now(timezone.utc).isoformat()}
            # Invalid responses are retained separately and never treated as canonical evidence.
            if "value" in locals():
                attempt["rejected_response"] = value
            if "raw_response" in locals():
                rejected_name = f"attempt-{state['failed_attempts']:05d}-raw.txt"
                _atomic(root / "evidence" / "rejections" / rejected_name, raw_response)
                attempt["raw_response_path"] = rejected_name
                attempt["raw_response_sha256"] = sha256(raw_response)
            _atomic(root / "evidence" / "rejections" / f"attempt-{state['failed_attempts']:05d}.json", _json(attempt))
            _save(root, state)
            raise PipelineError(str(exc)) from exc
        _atomic(root / "evidence" / "responses" / f"{job_id}.json", _json(value))
        state["jobs"][job_id].update(status="accepted", response_sha256=digest)
        state["last_error"] = None
        _save(root, state)
        try:
            _advance(root, state)
        except (OSError, ValueError) as exc:
            state["last_error"] = str(exc)
            _save(root, state)
            raise PipelineError(str(exc)) from exc
        return _status(root, state)


def validate(workspace) -> dict:
    root = Path(workspace).resolve()
    try:
        state = _load(root)
        errors = _check_evidence(root, state)
        entries = _canonical_entries(root, state)
        pages = _collected(root, state, "classification", "subpages")
        topics = _collected(root, state, "directory", "topics")
        if len({e["entry_id"] for e in entries}) != len(entries):
            errors.append("Duplicate global canonical entry IDs")
        if state["stage"] != "completed":
            errors.append("Pipeline incomplete; a completed Wiki cannot be published")
        else:
            primary = [e for page in pages for e in page["entry_ids"]]
            if len(primary) != len(set(primary)) or set(primary) != {e["entry_id"] for e in entries}:
                errors.append("Incomplete/duplicate global primary assignments")
            parents = [p for topic in topics for p in topic["subpage_ids"]]
            if len(parents) != len(set(parents)) or set(parents) != {p["subpage_id"] for p in pages}:
                errors.append("Incomplete/duplicate parent topic assignments")
            for relative, expected in (("evidence/entries.json", entries), ("derived/subpages.json", pages), ("derived/topics.json", topics),
                                       ("derived/overview.json", _collected(root, state, "overview", "statements"))):
                if _read(root / relative) != expected:
                    errors.append(f"Changed canonical/derived artifact: {relative}")
            errors.extend(export.validate_export(root / "wiki"))
            manifest = _read(root / "wiki" / "manifest.json")
            if manifest["entry_ids"] != [e["entry_id"] for e in entries] or manifest["subpage_ids"] != [p["subpage_id"] for p in pages]:
                errors.append("Wiki export IDs do not match accepted evidence")
        return {"valid": not errors, "completed": state["stage"] == "completed", "errors": errors,
                "counts": {"documents": len(state["documents"]), "entries": len(entries), "subpages": len(pages), "topics": len(topics)}}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"valid": False, "completed": False, "errors": [str(exc)], "counts": {}}
