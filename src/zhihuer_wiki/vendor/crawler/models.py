"""Crawler data models."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

ContentKind = Literal["answer", "article", "pin"]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(slots=True)
class ContentItem:
    source: str
    kind: ContentKind
    id: str
    title: str
    url: str
    author_token: str | None
    content_html: str
    content_text: str
    fetched_at: str = field(default_factory=utc_now_iso)
    created_at: str | None = None
    updated_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


@dataclass(slots=True)
class CreatorProfile:
    source: str
    author_token: str
    nickname: str
    profile_url: str
    avatar_url: str | None = None
    headline: str | None = None
    fetched_at: str = field(default_factory=utc_now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)
