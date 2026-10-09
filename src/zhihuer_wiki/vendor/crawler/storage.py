"""Small storage helpers for crawler artifacts."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Protocol


class JsonLine(Protocol):
    def to_json(self) -> str: ...


JsonlItem = JsonLine | Mapping[str, Any]


def write_jsonl(items: Iterable[JsonlItem], path: Path, *, append: bool = False) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    count = 0
    with path.open(mode, encoding="utf-8", newline="\n") as handle:
        for item in items:
            if isinstance(item, Mapping):
                handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True))
            else:
                handle.write(item.to_json())
            handle.write("\n")
            count += 1
    return count
