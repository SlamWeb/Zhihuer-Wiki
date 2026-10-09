"""Zhihu public API parsing and best-effort public crawling."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from html import unescape
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

from zhihuer_wiki.vendor.crawler.exceptions import CrawlBlocked, CrawlError, SourceFormatChanged
from zhihuer_wiki.vendor.crawler.models import ContentItem, ContentKind, CreatorProfile, utc_now_iso

ZH_DOMAIN = "https://www.zhihu.com"
PROFILE_API = f"{ZH_DOMAIN}/api/v4/members/{{token}}"
PROFILE_INCLUDE = "answer_count,articles_count,pins_count"
ANSWER_API = f"{ZH_DOMAIN}/api/v4/members/{{token}}/answers"
ARTICLE_API = f"{ZH_DOMAIN}/api/v4/members/{{token}}/articles"
ARTICLE_DETAIL_API = f"{ZH_DOMAIN}/api/v4/articles/{{article_id}}"
PIN_API = f"{ZH_DOMAIN}/api/v4/members/{{token}}/pins"

ANSWER_INCLUDE = (
    "data[*].id,content,excerpt,created_time,updated_time,voteup_count,"
    "comment_count,question.id,question.title,author.url_token"
)
ARTICLE_INCLUDE = (
    "data[*].id,title,content,excerpt,created,updated,created_time,updated_time,"
    "voteup_count,comment_count,author.url_token"
)
ARTICLE_DETAIL_INCLUDE = (
    "id,title,content,excerpt,created,updated,created_time,updated_time,"
    "voteup_count,comment_count,author.url_token"
)
PIN_INCLUDE = (
    "data[*].id,url,content,excerpt_title,created,updated,comment_count,"
    "reaction_count,like_count,repin_count,author.url_token"
)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}


def parse_user_token(value: str) -> str:
    """Accept only an unambiguous Zhihu URL token or profile URL."""
    if not isinstance(value, str):
        raise ValueError("Provide a Zhihu URL token or profile URL, not a display nickname.")
    raw = value.strip()
    if "://" in raw:
        parsed = urlparse(raw)
        if (parsed.scheme not in {"http", "https"} or parsed.netloc not in {"www.zhihu.com", "zhihu.com"}
                or parsed.username or parsed.password):
            raise ValueError("Expected a profile URL hosted on zhihu.com.")
        match = re.fullmatch(r"/people/([A-Za-z0-9][A-Za-z0-9_-]{0,99})(?:/(?:answers|posts|pins|activities))?/?", parsed.path)
        if not match:
            raise ValueError("Expected /people/<URL-token> in the Zhihu profile URL.")
        return match.group(1)
    raw = raw.removeprefix("@")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", raw):
        raise ValueError("Provide the ASCII profile URL token; display nicknames and paths are not accepted.")
    return raw


def validate_api_url(url: str) -> None:
    parsed = urlparse(url)
    if (parsed.scheme != "https" or parsed.netloc != "www.zhihu.com"
            or not parsed.path.startswith("/api/v4/") or parsed.username or parsed.password):
        raise SourceFormatChanged("Zhihu returned an unexpected API pagination URL.")


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(unescape(html or ""), "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")

    lines = [line.strip() for line in soup.get_text("\n").splitlines()]
    return "\n".join(line for line in lines if line)


def timestamp_to_iso(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, str) and not value.isdigit():
        return value
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(timestamp, timezone.utc).replace(microsecond=0).isoformat()


def answer_payload_to_item(
    payload: dict[str, Any],
    *,
    author_token: str | None,
    fetched_at: str | None = None,
) -> ContentItem:
    answer_id = str(payload.get("id") or payload.get("answer_id") or "")
    if not re.fullmatch(r"[0-9]+", answer_id):
        raise SourceFormatChanged("Answer payload is missing id.")

    question = payload.get("question") or {}
    question_id = question.get("id") or payload.get("question_id")
    title = question.get("title") or payload.get("title") or f"Zhihu answer {answer_id}"
    content_html = _full_content_html(payload, kind="answer")
    # API detail endpoints are not portable human-readable source citations.
    url = _answer_url(question_id=question_id, answer_id=answer_id)

    return ContentItem(
        source="zhihu",
        kind="answer",
        id=answer_id,
        title=str(title),
        url=url,
        author_token=_author_token(payload, fallback=author_token),
        content_html=content_html,
        content_text=html_to_text(content_html),
        fetched_at=fetched_at or utc_now_iso(),
        created_at=timestamp_to_iso(payload.get("created_time") or payload.get("created")),
        updated_at=timestamp_to_iso(payload.get("updated_time") or payload.get("updated")),
        metadata=_compact(
            {
                "question_id": question_id,
                "voteup_count": payload.get("voteup_count"),
                "comment_count": payload.get("comment_count"),
                "excerpt": html_to_text(payload.get("excerpt") or ""),
            }
        ),
    )


def article_payload_to_item(
    payload: dict[str, Any],
    *,
    author_token: str | None,
    fetched_at: str | None = None,
) -> ContentItem:
    article_id = str(payload.get("id") or payload.get("article_id") or "")
    if not re.fullmatch(r"[0-9]+", article_id):
        raise SourceFormatChanged("Article payload is missing id.")

    title = payload.get("title") or f"Zhihu article {article_id}"
    content_html = _full_content_html(payload, kind="article")
    url = f"https://zhuanlan.zhihu.com/p/{article_id}"

    return ContentItem(
        source="zhihu",
        kind="article",
        id=article_id,
        title=str(title),
        url=url,
        author_token=_author_token(payload, fallback=author_token),
        content_html=content_html,
        content_text=html_to_text(content_html),
        fetched_at=fetched_at or utc_now_iso(),
        created_at=timestamp_to_iso(payload.get("created") or payload.get("created_time")),
        updated_at=timestamp_to_iso(payload.get("updated") or payload.get("updated_time")),
        metadata=_compact(
            {
                "voteup_count": payload.get("voteup_count"),
                "comment_count": payload.get("comment_count"),
                "excerpt": html_to_text(payload.get("excerpt") or ""),
            }
        ),
    )


def pin_payload_to_item(
    payload: dict[str, Any],
    *,
    author_token: str | None,
    fetched_at: str | None = None,
) -> ContentItem:
    pin_id = str(payload.get("id") or payload.get("pin_id") or "")
    if not re.fullmatch(r"[0-9]+", pin_id):
        raise SourceFormatChanged("Pin payload is missing id.")

    content_html = pin_content_html(payload.get("content"))
    title = payload.get("excerpt_title") or html_to_text(content_html)[:40] or f"Zhihu pin {pin_id}"
    url = pin_url(payload.get("url"), pin_id=pin_id)

    return ContentItem(
        source="zhihu",
        kind="pin",
        id=pin_id,
        title=str(title),
        url=url,
        author_token=_author_token(payload, fallback=author_token),
        content_html=content_html,
        content_text=html_to_text(content_html),
        fetched_at=fetched_at or utc_now_iso(),
        created_at=timestamp_to_iso(payload.get("created") or payload.get("created_time")),
        updated_at=timestamp_to_iso(payload.get("updated") or payload.get("updated_time")),
        metadata=_compact(
            {
                "comment_count": payload.get("comment_count"),
                "reaction_count": payload.get("reaction_count"),
                "like_count": payload.get("like_count"),
                "repin_count": payload.get("repin_count"),
                "excerpt": html_to_text(payload.get("excerpt_title") or ""),
            }
        ),
    )


def profile_payload_to_profile(
    payload: dict[str, Any],
    *,
    author_token: str,
    fetched_at: str | None = None,
) -> CreatorProfile:
    token = str(payload.get("url_token") or author_token)
    nickname = str(payload.get("name") or token)
    return CreatorProfile(
        source="zhihu",
        author_token=token,
        nickname=nickname,
        profile_url=f"{ZH_DOMAIN}/people/{token}",
        avatar_url=payload.get("avatar_url") or payload.get("avatar_url_template"),
        headline=payload.get("headline"),
        fetched_at=fetched_at or utc_now_iso(),
        metadata=_compact(
            {
                "id": payload.get("id"),
                "gender": payload.get("gender"),
                "url": payload.get("url"),
                "avatar_url_template": payload.get("avatar_url_template"),
                "answer_count": payload.get("answer_count"),
                "articles_count": payload.get("articles_count"),
                "pins_count": payload.get("pins_count"),
            }
        ),
    )


def fallback_profile(token: str) -> CreatorProfile:
    return CreatorProfile(
        source="zhihu",
        author_token=token,
        nickname=token,
        profile_url=f"{ZH_DOMAIN}/people/{token}",
    )


def pin_content_html(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""

    blocks: list[str] = []
    for block in value:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("content") or block.get("own_text") or ""
            if text:
                blocks.append(f"<p>{text}</p>")
        elif block_type == "image":
            image_url = block.get("url") or block.get("src")
            if image_url:
                blocks.append(f'<p><img src="{image_url}" alt="image" /></p>')
        else:
            text = block.get("content") or block.get("own_text") or ""
            if text:
                blocks.append(f"<p>{text}</p>")
    return "\n".join(blocks)


def pin_url(raw_url: Any, *, pin_id: str) -> str:
    return f"{ZH_DOMAIN}/pins/{pin_id}"


def extract_items_from_initial_data(
    html: str,
    *,
    author_token: str | None = None,
    kinds: Iterable[ContentKind] = ("answer", "article", "pin"),
) -> list[ContentItem]:
    """Extract explicitly attributed full sources from a detail page.

    A detail page can embed recommendations. ``author_token`` filters the actual
    source authors; it is never a fallback identity for these unscoped entities.
    """

    payload = _load_initial_data(html)
    entities = payload.get("initialState", {}).get("entities", {})
    wanted = set(kinds)
    fetched_at = utc_now_iso()
    items: list[ContentItem] = []

    converters = {"answer": answer_payload_to_item, "article": article_payload_to_item, "pin": pin_payload_to_item}
    collections = {"answer": "answers", "article": "articles", "pin": "pins"}
    for kind in ("answer", "article", "pin"):
        if kind not in wanted:
            continue
        for item_payload in (entities.get(collections[kind]) or {}).values():
            if not isinstance(item_payload, dict):
                continue
            actual_author = _author_token(item_payload, fallback=None)
            if not actual_author or (author_token is not None and actual_author != author_token):
                continue
            items.append(converters[kind](item_payload, author_token=None, fetched_at=fetched_at))

    return items


class ZhihuPublicCrawler:
    """Best-effort zero-touch crawler for public Zhihu endpoints."""

    def __init__(
        self,
        *,
        delay_seconds: float = 1.5,
        max_api_pages: int | None = 10,
        progress: Any | None = None,
    ) -> None:
        self.delay_seconds = delay_seconds
        self.max_api_pages = max_api_pages
        self.progress = progress
        self.outcomes: dict[str, str] = {}
        self._last_request = 0.0

    def iter_kind(self, token: str, *, kind: ContentKind, limit: int | None) -> Iterator[ContentItem]:
        yield from self._crawl_kind(parse_user_token(token), kind=kind, limit=limit)

    def crawl_profile(self, user: str) -> CreatorProfile:
        token = parse_user_token(user)
        payload = self._get_json(
            PROFILE_API.format(token=token),
            params={"include": PROFILE_INCLUDE},
            referer=f"{ZH_DOMAIN}/people/{token}",
        )
        return profile_payload_to_profile(payload, author_token=token)

    def crawl_user(
        self,
        user: str,
        *,
        kinds: Iterable[ContentKind] = ("article", "answer", "pin"),
        max_items: int | None = 100,
    ) -> list[ContentItem]:
        token = parse_user_token(user)
        items: list[ContentItem] = []
        seen: set[tuple[str, str]] = set()
        errors: list[str] = []

        for kind in tuple(kinds):
            remaining = None if max_items is None else max_items - len(items)
            if remaining is not None and remaining <= 0:
                break
            try:
                for item in self._crawl_kind(token, kind=kind, limit=remaining):
                    key = (item.kind, item.id)
                    if key in seen:
                        continue
                    seen.add(key)
                    items.append(item)
                    if max_items is not None and len(items) >= max_items:
                        return items
            except CrawlError as exc:
                errors.append(f"{kind}: {exc}")
                self._progress(f"public {kind} failed: {exc}")

        if not items and errors:
            raise CrawlBlocked("; ".join(errors))
        return items

    def _crawl_kind(self, token: str, *, kind: ContentKind, limit: int | None) -> Iterator[ContentItem]:
        self.outcomes[kind] = "running"
        if kind == "answer":
            url = ANSWER_API.format(token=token)
            params = {"include": ANSWER_INCLUDE, "offset": 0, "limit": min(20, limit or 20), "sort_by": "created"}
            converter = answer_payload_to_item
        elif kind == "article":
            url = ARTICLE_API.format(token=token)
            params = {"include": ARTICLE_INCLUDE, "offset": 0, "limit": min(20, limit or 20), "sort_by": "created"}
            converter = article_payload_to_item
        elif kind == "pin":
            url = PIN_API.format(token=token)
            params = {"include": PIN_INCLUDE, "offset": 0, "limit": min(20, limit or 20)}
            converter = pin_payload_to_item
        else:
            raise ValueError(f"Unsupported Zhihu content kind: {kind}")

        referer = f"{ZH_DOMAIN}/people/{token}/{profile_path_for_kind(kind)}"
        yielded = 0
        page_index = 0
        seen_page_urls: set[str] = set()
        while self.max_api_pages is None or page_index < self.max_api_pages:
            if url in seen_page_urls:
                raise SourceFormatChanged("Zhihu pagination entered a repeated page.")
            seen_page_urls.add(url)
            payload = self._get_json(url, params=params, referer=referer)
            data = payload.get("data")
            if not isinstance(data, list):
                raise SourceFormatChanged("Zhihu API response is missing data list.")
            self._progress(f"public {kind} page {page_index + 1}: {len(data)} item(s)")
            for item_payload in data:
                if not isinstance(item_payload, dict):
                    continue
                if kind == "article" and not (item_payload.get("content") or item_payload.get("content_html")) and item_payload.get("id"):
                    if not str(item_payload["id"]).isdigit():
                        raise SourceFormatChanged("Article payload contained an invalid identity.")
                    try:
                        detail = self._get_json(
                            ARTICLE_DETAIL_API.format(article_id=item_payload["id"]),
                            params={"include": ARTICLE_DETAIL_INCLUDE},
                            referer=f"https://zhuanlan.zhihu.com/p/{item_payload['id']}",
                        )
                        item_payload = {**item_payload, **detail}
                    except CrawlError as exc:
                        raise SourceFormatChanged("Full article content could not be fetched.") from exc
                item = converter(item_payload, author_token=token)
                if not item.content_text:
                    raise SourceFormatChanged("Zhihu returned an item without full content.")
                yield item
                yielded += 1
                if limit is not None and yielded >= limit:
                    self.outcomes[kind] = "limited"
                    return

            paging = payload.get("paging") or {}
            if paging.get("is_end"):
                self.outcomes[kind] = "completed"
                return
            next_url = paging.get("next")
            if not next_url:
                raise SourceFormatChanged("Zhihu response did not confirm the end of pagination.")
            if next_url == url:
                raise SourceFormatChanged("Zhihu returned a repeated pagination URL.")
            validate_api_url(next_url)
            url = next_url
            params = None
            page_index += 1
            self._sleep()
        self.outcomes[kind] = "page_limit"

    def _get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        referer: str,
    ) -> dict[str, Any]:
        validate_api_url(url)
        if params:
            url = f"{url}?{urlencode(params)}"
        headers = {**DEFAULT_HEADERS, "Referer": referer}
        request = Request(url, headers=headers)
        for attempt in range(3):
            self._throttle()
            try:
                with urlopen(request, timeout=20) as response:
                    text = response.read().decode("utf-8", errors="replace")
                    status = response.status
                break
            except HTTPError as exc:
                if exc.code in {401, 403, 429}:
                    raise CrawlBlocked(f"Zhihu API returned HTTP {exc.code}; login, rate limit or verification may be required.") from None
                if exc.code in {500, 502, 503, 504} and attempt < 2:
                    self._sleep()
                    continue
                raise CrawlError(f"Zhihu API returned HTTP {exc.code}.") from None
            except (URLError, TimeoutError, OSError):
                if attempt < 2:
                    self._sleep()
                    continue
                raise CrawlError("Could not reach Zhihu API after bounded retries.") from None

        if status >= 400:
            raise CrawlError(f"Zhihu API returned HTTP {status} for {url}")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            reason = blocked_reason_from_text(text)
            if reason:
                raise CrawlBlocked(f"Zhihu API returned {reason}.") from exc
            raise SourceFormatChanged("Zhihu API returned non-JSON content.") from exc
        if not isinstance(payload, dict):
            raise SourceFormatChanged("Zhihu API returned a non-object JSON payload.")
        if "error" in payload:
            error_text = json.dumps(payload["error"], ensure_ascii=False)
            reason = blocked_reason_from_text(error_text)
            if reason:
                raise CrawlBlocked(f"Zhihu API returned {reason}.")
            raise CrawlError("Zhihu API returned an error object.")
        return payload

    def _throttle(self) -> None:
        remaining = self.delay_seconds - (time.monotonic() - self._last_request)
        if remaining > 0:
            time.sleep(remaining)
        self._last_request = time.monotonic()

    def _sleep(self) -> None:
        if self.delay_seconds > 0:
            import time

            time.sleep(self.delay_seconds)

    def _progress(self, message: str) -> None:
        if self.progress is not None:
            self.progress(message)


def blocked_reason_from_text(text: str) -> str | None:
    lowered = text.lower()
    markers = {
        "login": "login required",
        "登录": "login required",
        "验证码": "captcha",
        "captcha": "captcha",
        "安全验证": "verification",
        "异常流量": "anti-abuse",
        "系统繁忙": "system busy",
        "authenticationinvalidclient": "authentication invalid",
        "forbidden": "forbidden",
    }
    for marker, reason in markers.items():
        if marker.lower() in lowered:
            return reason
    return None


def _load_initial_data(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    script = soup.find("script", id="js-initialData")
    if script is None or script.string is None:
        return {"initialState": {"entities": {}}}
    try:
        payload = json.loads(script.string)
    except json.JSONDecodeError as exc:
        raise CrawlError("Could not parse Zhihu initial data JSON.") from exc
    if not isinstance(payload, dict):
        raise SourceFormatChanged("Zhihu initial data was not a JSON object.")
    return payload


def profile_path_for_kind(kind: ContentKind) -> str:
    if kind == "answer":
        return "answers"
    if kind == "article":
        return "posts"
    if kind == "pin":
        return "pins"
    raise ValueError(f"Unsupported Zhihu content kind: {kind}")


def _answer_url(*, question_id: Any, answer_id: str) -> str:
    if question_id:
        return f"{ZH_DOMAIN}/question/{question_id}/answer/{answer_id}"
    return f"{ZH_DOMAIN}/answer/{answer_id}"


def _author_token(payload: dict[str, Any], *, fallback: str | None) -> str | None:
    author = payload.get("author") or {}
    if isinstance(author, dict):
        return author.get("url_token") or fallback
    return fallback


def _full_content_html(payload: dict[str, Any], *, kind: str) -> str:
    """Excerpt fields are metadata and never a substitute for a full source."""
    content = payload.get("content") or payload.get("content_html")
    if not isinstance(content, str) or not html_to_text(content).strip():
        raise SourceFormatChanged(f"{kind.capitalize()} payload is missing full content.")
    return content


def _compact(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value not in (None, "")}
