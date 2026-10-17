"""Offline portable Wiki v2 helper. Run directly without installation."""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from zhihuer_wiki.folder import render_wiki, validate_wiki


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("render", "validate"))
    parser.add_argument("--wiki", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = (render_wiki if args.command == "render" else validate_wiki)(args.wiki)
    except (OSError, ValueError) as exc:
        report = {"valid": False, "errors": str(exc).splitlines(), "warnings": [], "counts": {}}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
