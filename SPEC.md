# Zhihuer-Wiki contract

Status: standalone V1 verified on 2026-10-09 with 113 offline tests and a clean-clone, real Zhihu-to-Codex-Luna smoke run. This is a product extraction, not a second PersonClone research checkout. Small-sample evidence does not validate full-corpus semantic quality or the real embedding download path; see docs/verification.md.

Input: a Zhihu profile URL or URL token (a display nickname is not a unique identifier), or an existing crawler Markdown directory. Output: a local, portable Wiki folder.

The repository does not call any LLM HTTP API or read LLM API keys. Semantic work is performed by the user's already available coding agent. Codex CLI is an optional adapter; other agents consume the same file jobs through `next` / `submit`.

Pipeline: crawl -> immutable source snapshot -> full entry extraction in independent document batches -> offline candidate grouping -> full semantic classification of every entry -> global directory arrangement -> source-bound overview -> checked Markdown export.

Canonical entries preserve paraphrase, exact source quotes, source document ID, date, context/conditions, and topic hint. Same-question opposing or historical expressions stay separate entries. Navigation is a derived overview -> topic -> subpage view; multiple references retain one canonical ID. No hard page count, no recursive neutralization, no loss of source evidence.

Read-only query contract: an existing exported Wiki is navigated by the user's coding agent using ordinary file reads, directory listing and text search. The Skill and docs/reading.md describe progressive navigation, source verification, time/condition distinctions and evidence-based stopping. Queries do not trigger crawling, alter immutable exports, or load the entire readable.md by default. This is documented agent behavior, not a built-in search runtime or a quality-validated retrieval algorithm.

Small corpora skip numerical pre-grouping. Larger corpora default to local sentence-transformer embeddings + KMeans; an explicitly selected lexical TF-IDF fallback is labelled as such. Candidate groups are not final topics. Classification jobs include every assigned entry with quotes and context. Directory stage arranges intact subpages; it does not merge their contents just because labels are similar.

Job protocol: `pipeline.start(workspace, raw_dir, author, **settings)` creates deterministic jobs. `pipeline.next_jobs(workspace)` returns pending jobs with `job_id`, `stage`, `prompt_path`, `schema_path`, `response_path`. `pipeline.submit(workspace, job_id, response_path)` validates and records a response atomically; `pipeline.status(workspace)` summarizes progress. Completion publishes `workspace/wiki/` with index.md, overview.md, topics/, subpages/, entries/, sources/ and readable.md. Failed/incomplete jobs never publish a completed Wiki. Validate rejects invented quotes, missing IDs, duplicate primary assignments, invalid paths and references. Repeated accepted responses are idempotent; changed sources require a new workspace.

Crawler contract: standalone copy adapted from PersonClone's crawler modules, with no dependency on personaforge. `crawl(user, out_dir, *, max_items=None, storage_state=None, kinds=("answer", "article", "pin"), delay_seconds=1.5)` returns an auditable summary. Public endpoints first; browser fallback with optional local login state; existing Markdown import is supported. No bypass of login challenges. `login(storage_state)` explicitly opens a browser for the human to log in. No auth payload appears in model jobs, trace, public examples or Git.

CLI: `python -m zhihuer_wiki build USER [--raw-dir DIR] [--out DIR] [--agent codex|manual] [--max-items N] [--storage-state FILE] [--model MODEL]`; manual mode prepares jobs. Agent mode drives next/submit and must fail if no output or invalid output, rather than generating a mock Wiki. `next --workspace DIR`, `submit --workspace DIR --job ID --response FILE`, `status --workspace DIR`, `validate --workspace DIR`, `login [--storage-state FILE]`.

Default data location: outputs/<author>/; credentials under .local/auth/. Git ignores these, model caches and all runtime artifacts. Checked-in samples are invented and clearly labelled. Live crawl tests and genuine agent runs are separate from deterministic fixture tests. Reports must state the distinction.
