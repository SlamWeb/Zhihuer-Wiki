"""Legacy V1 bootstrap; new Agent-first V2 builds follow docs/build-with-agent.md."""
from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import sys
import venv


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in {"-h", "--help"}:
        print("Legacy V1 job runner. New Agent-first V2: read .agents/skills/zhihuer-wiki/SKILL.md.\n"
              "Usage: python run.py ZHIHU_PROFILE_OR_TOKEN [build options]\n"
              "Creates a local .venv, installs dependencies, and builds with your logged-in Codex.\n"
              "Use --agent manual for your current agent; --grouping none/tfidf avoids embedding extras.\n"
              "For full CLI help: python -m zhihuer_wiki build --help after installation.")
        return 0
    if sys.version_info < (3, 11):
        print("Python 3.11+ is required.", file=sys.stderr)
        return 1
    print("[V1] 旧版自动任务运行器；Agent 直接建 V2 请读 docs/build-with-agent.md。", flush=True)
    root = Path(__file__).resolve().parent
    environment = root / ".venv"
    python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not python.is_file():
        print("[准备] 创建项目本地 .venv", flush=True)
        venv.EnvBuilder(with_pip=True).create(environment)
    grouping = "embedding"
    for i, value in enumerate(argv):
        if value == "--grouping" and i + 1 < len(argv):
            grouping = argv[i + 1]
        elif value.startswith("--grouping="):
            grouping = value.split("=", 1)[1]
    install_target = str(root) + ("[semantic]" if grouping == "embedding" else "")
    signature = hashlib.sha256((root / "pyproject.toml").read_bytes() + install_target.encode()).hexdigest()
    marker = environment / ".zhihuer-installed"
    if not marker.is_file() or marker.read_text(encoding="utf-8") != signature:
        print("[准备] 安装项目依赖；默认语义聚类首次安装可能较慢", flush=True)
        install = subprocess.run([str(python), "-m", "pip", "install", "-e", install_target], cwd=root)
        if install.returncode:
            return install.returncode
        marker.write_text(signature, encoding="utf-8")
    return subprocess.run([str(python), "-m", "zhihuer_wiki", "build", *argv], cwd=root).returncode


if __name__ == "__main__":
    raise SystemExit(main())
