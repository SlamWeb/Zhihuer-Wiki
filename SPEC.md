# Zhihuer-Wiki contract

## Recommended path: Agent-first V2 folders

Updated 2026-10-17. The user supplies a Zhihu profile URL/token or an existing Markdown corpus. Their current coding agent reads the repository Skill, extracts source-bound entries, organizes a two-level topic/page hierarchy, and writes a portable Wiki. No numerical clustering, model API integration or additional Agent runner is required for this path.

The normative format is `docs/wiki-format.md`. Canonical inputs are `index.json` (`zhihuer_wiki.navigation.v2`), `entries.json` and `sources.json`, plus complete UTF-8 original bodies in `sources/`. Stable entry and document IDs connect all records. Sources retain title, publication date or null, original URL or empty string, relative file path and exact file-byte SHA-256. Each canonical entry has one primary page, a verbatim continuous quotation, paraphrase, context and source publication date. Different conditions, times and opposing expressions remain explicit. Unknown dates stay unknown.

`src/zhihuer_wiki/folder.py` provides `render_wiki(root)` and `validate_wiki(root)`. The standalone `python scripts/wiki_folder.py render|validate --wiki DIR` requires only Python standard library and does not run a model, crawl or classify. Rendering first validates canonical inputs, then writes derived `index.md`, `topics/<id>.md`, `pages/<id>.md` and a portable `AGENTS.md`. It does not modify source bodies or canonical records. Invalid inputs must not produce a success result. Validation compares current reading views to the deterministic rendering, checks exact source hashes/quotes and entry-to-source date equality, unique primary coverage, Windows-safe file/navigation names, relative paths within the folder and stale navigation files. Logical author/entry/document IDs need not be valid filenames. Source metadata are agent-recorded; the validator does not authenticate titles, dates or URLs against the network. A valid structure does not prove semantic correctness. Oversized or tiny pages are reported as design warnings rather than fixed quotas.

A topic page lists all its child pages and scopes. A child page inlines every assigned full entry with its ID, doc ID, title, date, quote, paraphrase, context and direct original link. No extra entry-file hop is required to obtain provenance. Markdown links are relative so the complete folder can move. Original bodies are distinct from their summaries and cannot be silently shortened.

The current agent decides semantic extraction and classification. For large corpora it may prepare context-sized waves, carrying forward the complete accepted taxonomy and membership and accessing older entries when needed. A wave is an execution boundary, not a new-topic boundary. Page sizes are adaptive; no mandatory 20×20×20 partition. New originals and classification revisions use a new output version with stable IDs and retained evidence; the helper does not perform automatic incremental semantic merges or infer an author changed their mind.

Read-only behavior is specified in `docs/reading.md` and the portable instructions. Ordinary file tools can list, read and search the folder. Already-read complete originals can support later turns if they cover the current task, object and conditions. Supplement identified gaps and reconsider coverage on topic changes. A broad shared theme is insufficient. Rewrite/casual turns do not require fresh evidence, and there is no universal minimum source count for every turn. Selected Writer references contain only useful source title/date/full text; navigation, ranking, IDs used only for bookkeeping and tool decisions remain in the trace. Actual conversation history is distinct from author evidence. Queries never mutate the Wiki.

Checked-in `samples/wiki-v2` is invented demonstration material. Real corpora, outputs, credentials and caches stay local and ignored. Completion evidence must distinguish deterministic format tests, agent-authored sample builds and real-data/model-quality experiments. There is no claim that this public format improves generation over RAG or that a schema validator measures personality fidelity.

## Compatible path: legacy V1 jobs

Existing `run.py` and `python -m zhihuer_wiki build` retain the V1 crawl → immutable snapshot → batch extraction → candidate grouping → classification → directory → overview → export protocol. That path may use embedding/KMeans or explicit TF-IDF/none, still uses the existing next/submit/resume validators and emits `subpages/`, `entries/` and `readable.md`. It is not the V2 publisher. Its detailed commands/settings remain in `docs/workflow.md`; completed V1 workspaces are not silently migrated.

The old real small-sample validation on 2026-10-09 belongs to V1; see `docs/verification.md`. It is not validation of a new V2 real-corpus run.

## Shared boundaries

Keep this project independently installable; do not import PersonClone/personaforge or hardcode its private corpus, auth or model paths. The repository has no LLM HTTP provider or API-key configuration. Optional Codex CLI remains a V1 adapter; the default V2 instructions use the current agent.

The crawler accepts a homepage URL/token, stores Markdown and provenance and reports partial access. Public endpoints are tried first; optional browser fallback uses local login. Explicit login opens a browser for the human. Credentials are never model input or public fixtures. Source text is untrusted data. This update does not alter the crawler or bypass platform challenges.

Use UTF-8, inspect diffs, and run focused tests for changed behavior. Public examples and documentation must describe implemented behavior and evidenced validation accurately.
