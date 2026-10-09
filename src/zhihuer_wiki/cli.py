"""Explainable CLI for crawling, agent file jobs and portable Wiki export."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def _json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="知乎主页 → 原话条目 → 主题子页 → Persona Wiki；使用已有 Agent，无 LLM API key。")
    commands = root.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="输入主页 URL/URL token，抓取后由 Agent 构建 Wiki")
    build.add_argument("user", help="知乎 URL token 或 /people/ 主页网址；不是显示昵称")
    build.add_argument("--out", type=Path, help="独立输出工作区；默认 outputs/<author>/")
    build.add_argument("--raw-dir", type=Path, help="复用已有 crawler Markdown，跳过网络抓取")
    build.add_argument("--agent", choices=("codex", "manual"), default="codex")
    build.add_argument("--storage-state", type=Path, default=Path(".local/auth/zhihu_storage_state.json"))
    build.add_argument("--max-items", type=int, help="只处理最多 N 篇；默认抓取所有可达内容")
    build.add_argument("--kinds", default="answer,article,pin")
    build.add_argument("--allow-partial", action="store_true", help="明确允许用部分抓取结果构建；报告仍标记缺失")
    build.add_argument("--model", help="可选：透传本机 Codex 支持的模型名；默认使用其当前设置")
    build.add_argument("--grouping", choices=("embedding", "tfidf", "none"), default="embedding")
    build.add_argument("--embedding-model", default="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    build.add_argument("--batch-docs", type=int, default=10)
    build.add_argument("--batch-tokens", type=int, default=20000)
    build.add_argument("--classification-entries", type=int, default=100)
    build.add_argument("--timeout", type=float, default=900, help="每个 Agent 任务的最大秒数")
    resume = commands.add_parser("resume", help="从已通过核验的检查点继续，不重跑成功任务")
    resume.add_argument("--workspace", type=Path, required=True)
    resume.add_argument("--agent", choices=("codex", "manual"), default="codex")
    resume.add_argument("--model")
    resume.add_argument("--timeout", type=float, default=900)
    for name in ("next", "status", "validate"):
        p = commands.add_parser(name)
        p.add_argument("--workspace", type=Path, required=True)
    submit = commands.add_parser("submit")
    submit.add_argument("--workspace", type=Path, required=True)
    submit.add_argument("--job", required=True)
    submit.add_argument("--response", type=Path, required=True)
    login = commands.add_parser("login", help="打开本机浏览器，由用户登录知乎，保存本地状态")
    login.add_argument("--storage-state", type=Path, default=Path(".local/auth/zhihu_storage_state.json"))
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        from . import pipeline
        if args.command == "login":
            from .crawler import login
            login(args.storage_state)
            print("知乎登录状态已保存在本机。无需把 Cookie 发给 Agent。")
            return 0
        if args.command == "build":
            from .crawler import crawl, parse_user_token
            author = parse_user_token(args.user)
            workspace = (args.out or Path("outputs") / author).resolve()
            if args.batch_docs <= 0 or args.batch_tokens <= 0 or args.classification_entries <= 0:
                raise ValueError("批次大小必须是正数。")
            if args.max_items is not None and args.max_items <= 0:
                raise ValueError("--max-items 必须是正数。")
            if (workspace / ".work" / "state.json").exists():
                raise ValueError("输出工作区已有任务，请用 resume；新原文请使用新的 --out。")
            raw_dir = args.raw_dir.resolve() if args.raw_dir else workspace / "raw"
            if args.raw_dir is None:
                kinds = tuple(x.strip() for x in args.kinds.split(",") if x.strip())
                if not kinds or any(x not in {"answer", "article", "pin"} for x in kinds):
                    raise ValueError("--kinds 只接受 answer,article,pin。")
                state = args.storage_state if args.storage_state.is_file() else None
                print(f"[抓取] {author} → {raw_dir}", file=sys.stderr)
                summary = crawl(args.user, raw_dir, max_items=args.max_items,
                                storage_state=state, kinds=kinds)
                workspace.mkdir(parents=True, exist_ok=True)
                (workspace / "crawl-report.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"[抓取] 本次 {summary['fetched_count']} 篇，状态 {summary['status']}", file=sys.stderr)
                if not summary["total_count"]:
                    raise ValueError("没有抓到可处理材料。需要时先运行 login，再重试。")
                if summary["status"] in {"partial", "failed"} and not args.allow_partial:
                    raise ValueError("部分抓取失败，材料已保留。处理好登录/限流后重试，或显式 --allow-partial。")
            result = pipeline.start(
                workspace, raw_dir, author, extraction_batch_docs=args.batch_docs,
                extraction_max_tokens=args.batch_tokens,
                classification_batch_entries=args.classification_entries,
                classification_max_tokens=args.batch_tokens,
                grouping_backend=args.grouping, embedding_model=args.embedding_model,
            )
            print("[准备] 原文已快照，接下来逐批提炼并核验", file=sys.stderr)
            if args.agent == "codex":
                from .agent_runner import drive
                result = drive(workspace, model=args.model, timeout=args.timeout,
                               progress=lambda line: print(line, file=sys.stderr, flush=True))
            else:
                result = {"status": result, "jobs": pipeline.next_jobs(workspace),
                          "instruction": "让当前 Agent 读取每个任务的 prompt_path/input_path/schema_path，写响应后 submit，循环 next 到完成。"}
            _json(result)
        elif args.command == "resume":
            if args.agent == "manual":
                _json(pipeline.next_jobs(args.workspace))
            else:
                from .agent_runner import drive
                _json(drive(args.workspace, model=args.model, timeout=args.timeout,
                            progress=lambda line: print(line, file=sys.stderr, flush=True)))
        elif args.command == "next":
            _json(pipeline.next_jobs(args.workspace))
        elif args.command == "submit":
            _json(pipeline.submit(args.workspace, args.job, args.response))
        elif args.command == "status":
            _json(pipeline.status(args.workspace))
        elif args.command == "validate":
            result = pipeline.validate(args.workspace)
            _json(result)
            return 0 if result["valid"] else 1
        return 0
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
