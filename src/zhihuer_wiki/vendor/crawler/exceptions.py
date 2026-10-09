"""Crawler exception types."""

from __future__ import annotations


class CrawlError(RuntimeError):
    """Base class for crawler failures."""


class CrawlBlocked(CrawlError):
    """The source refused the request or requested login/verification."""


class SourceFormatChanged(CrawlError):
    """The source response shape no longer matches the parser contract."""
