"""Playwright-backed Zhihu crawler fallback."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from zhihuer_wiki.vendor.crawler.exceptions import CrawlBlocked, CrawlError, SourceFormatChanged
from zhihuer_wiki.vendor.crawler.models import ContentItem, ContentKind, utc_now_iso
from zhihuer_wiki.vendor.crawler.zhihu import (
    ANSWER_API,
    ANSWER_INCLUDE,
    ARTICLE_API,
    ARTICLE_DETAIL_API,
    ARTICLE_DETAIL_INCLUDE,
    ARTICLE_INCLUDE,
    PIN_API,
    PIN_INCLUDE,
    PROFILE_API,
    PROFILE_INCLUDE,
    ZH_DOMAIN,
    answer_payload_to_item,
    article_payload_to_item,
    blocked_reason_from_text,
    extract_items_from_initial_data,
    fallback_profile,
    html_to_text,
    parse_user_token,
    pin_payload_to_item,
    profile_payload_to_profile,
    profile_path_for_kind,
    validate_api_url,
)

ZH_SIGNIN_URL = "https://www.zhihu.com/signin?next=%2F"
ZH_LINK_SELECTOR = ",".join(
    [
        "a[data-za-detail-view-element_name='Title']",
        ".ContentItem-title a",
        "a[href*='/question/'][href*='/answer/']",
        "a[href*='zhuanlan.zhihu.com/p/']",
        "a[href*='/pins/']",
    ]
)

ANSWER_TITLE_SELECTORS = [
    ".QuestionHeader-title",
    ".QuestionHeader h1",
    "h1",
]
ANSWER_CONTENT_SELECTORS = [
    ".RichContent-inner",
    ".ContentItem .RichContent",
    ".RichText.ztext",
    ".RichText",
]
ARTICLE_TITLE_SELECTORS = [
    ".Post-Title",
    "article h1",
    "h1",
]
ARTICLE_CONTENT_SELECTORS = [
    ".Post-RichText",
    "article .RichText",
    ".RichText.ztext",
    ".RichText",
    "article",
]
PIN_TITLE_SELECTORS = [
    ".PinItem-title",
    ".ContentItem-title",
    "h1",
]
PIN_CONTENT_SELECTORS = [
    ".PinItem-content",
    ".PinItem .RichText",
    ".RichText.ztext",
    ".RichText",
    ".ContentItem",
]


class ZhihuBrowserCrawler:
    def __init__(
        self,
        *,
        headless: bool = True,
        storage_state: Path | None = None,
        delay_seconds: float = 1.5,
        use_api: bool = True,
        max_api_pages: int | None = 10,
        max_scrolls: int = 80,
        stable_rounds: int = 5,
        progress: Any | None = None,
    ) -> None:
        self.headless = headless
        self.storage_state = storage_state
        self.delay_seconds = delay_seconds
        self.use_api = use_api
        self.max_api_pages = max_api_pages
        self.max_scrolls = max_scrolls
        self.stable_rounds = stable_rounds
        self.progress = progress
        self.outcomes: dict[str, str] = {}
        self._last_request = 0.0

    def iter_kind(self, token: str, *, kind: ContentKind, limit: int | None) -> Iterator[ContentItem]:
        """Yield durably consumable items and retain the actual ending condition."""
        token = parse_user_token(token)
        self.outcomes[kind] = "running"
        seen: set[str] = set()
        api_empty_complete = False
        with self._playwright_context() as context:
            if self.use_api:
                try:
                    for item in self._crawl_api_items(context, token, kind=kind, limit=limit):
                        if item.id not in seen:
                            seen.add(item.id)
                            yield item
                    if seen and self.outcomes[kind] in {"completed", "limited"}:
                        return
                    api_empty_complete = not seen and self.outcomes[kind] == "completed"
                except CrawlError:
                    self.outcomes[kind] = "api_failed"
            page = context.new_page()
            links = self.collect_links(page, token, kind=kind, limit=limit)
            for link in links:
                item = self.extract_item(page, link, kind=kind, author_token=token)
                if item.id in seen:
                    continue
                seen.add(item.id)
                yield item
                if limit is not None and len(seen) >= limit:
                    self.outcomes[kind] = "limited"
                    return
            # A stable scroll is a discovery boundary, not evidence that the source
            # is exhausted. Only the API's explicit is_end is authoritative here.
            self.outcomes[kind] = "completed" if api_empty_complete and not seen else "dom_boundary"

    def crawl_profile(self, user: str):
        token = parse_user_token(user)
        with self._playwright_context() as context:
            try:
                payload = self._api_get_json(
                    context,
                    PROFILE_API.format(token=token),
                    params={"include": PROFILE_INCLUDE},
                    referer=f"{ZH_DOMAIN}/people/{token}",
                )
                return profile_payload_to_profile(payload, author_token=token)
            except CrawlError:
                return fallback_profile(token)

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

        with self._playwright_context() as context:
            page = context.new_page()
            for kind in tuple(kinds):
                kind_start_count = len(items)
                remaining = None if max_items is None else max_items - len(items)
                if remaining is not None and remaining <= 0:
                    break

                if self.use_api:
                    try:
                        for item in self._crawl_api_items(context, token, kind=kind, limit=remaining):
                            key = (item.kind, item.id)
                            if key in seen:
                                continue
                            seen.add(key)
                            items.append(item)
                            if max_items is not None and len(items) >= max_items:
                                return items
                        if len(items) > kind_start_count:
                            continue
                    except CrawlError as exc:
                        errors.append(f"{kind} api: {exc}")
                        self._progress(f"browser API {kind} failed: {exc}")

                try:
                    links = self.collect_links(page, token, kind=kind, limit=remaining)
                    for link in links:
                        item = self.extract_item(page, link, kind=kind, author_token=token)
                        key = (item.kind, item.id)
                        if key in seen:
                            continue
                        seen.add(key)
                        items.append(item)
                        if max_items is not None and len(items) >= max_items:
                            return items
                except CrawlError as exc:
                    errors.append(f"{kind} page: {exc}")
                    self._progress(f"browser page {kind} failed: {exc}")

        if not items and errors:
            raise CrawlBlocked("; ".join(errors))
        return items

    def _crawl_api_items(
        self,
        context: Any,
        token: str,
        *,
        kind: ContentKind,
        limit: int | None,
    ) -> Iterator[ContentItem]:
        url = api_url_for_kind(token, kind)
        params = api_params_for_kind(kind, limit=limit)
        referer = f"{ZH_DOMAIN}/people/{token}/{profile_path_for_kind(kind)}"
        yielded = 0
        self.outcomes[kind] = "running"
        page_index = 0
        seen_page_urls: set[str] = set()

        while self.max_api_pages is None or page_index < self.max_api_pages:
            if url in seen_page_urls:
                raise SourceFormatChanged("Zhihu pagination entered a repeated page.")
            seen_page_urls.add(url)
            payload = self._api_get_json(context, url, params=params, referer=referer)
            data = payload.get("data")
            if not isinstance(data, list):
                raise SourceFormatChanged("Zhihu API response is missing data list.")

            for item_payload in data:
                if not isinstance(item_payload, dict):
                    continue
                if kind == "answer":
                    item = answer_payload_to_item(item_payload, author_token=token)
                elif kind == "article":
                    if not (item_payload.get("content") or item_payload.get("content_html")) and item_payload.get("id"):
                        if not str(item_payload["id"]).isdigit():
                            raise SourceFormatChanged("Article payload contained an invalid identity.")
                        try:
                            detail = self._fetch_article_detail_api(context, str(item_payload["id"]))
                            item_payload = {**item_payload, **detail}
                        except CrawlError as exc:
                            raise SourceFormatChanged("Full article content could not be fetched.") from exc
                    item = article_payload_to_item(item_payload, author_token=token)
                elif kind == "pin":
                    item = pin_payload_to_item(item_payload, author_token=token)
                else:
                    raise ValueError(f"Unsupported Zhihu content kind: {kind}")
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

    def _fetch_article_detail_api(self, context: Any, article_id: str) -> dict[str, Any]:
        self._sleep()
        return self._api_get_json(
            context,
            ARTICLE_DETAIL_API.format(article_id=article_id),
            params={"include": ARTICLE_DETAIL_INCLUDE},
            referer=f"https://zhuanlan.zhihu.com/p/{article_id}",
        )

    def _api_get_json(
        self,
        context: Any,
        url: str,
        *,
        params: dict[str, Any] | None,
        referer: str,
    ) -> dict[str, Any]:
        validate_api_url(url)
        for attempt in range(3):
            remaining = self.delay_seconds - (time.monotonic() - self._last_request)
            if remaining > 0:
                time.sleep(remaining)
            self._last_request = time.monotonic()
            try:
                response = context.request.get(url, params=params, headers={"Referer": referer}, timeout=20_000)
                text = response.text()
            except Exception:
                if attempt < 2:
                    self._sleep()
                    continue
                raise CrawlError("Browser API request failed after bounded retries.") from None
            if response.status in {500, 502, 503, 504} and attempt < 2:
                self._sleep()
                continue
            break
        if response.status in {401, 403, 429}:
            reason = blocked_reason_from_text(text)
            raise CrawlBlocked(
                f"Zhihu API returned HTTP {response.status}"
                + (f" ({reason})" if reason else "; login, rate limit, or verification may be required")
            )
        if response.status >= 400:
            raise CrawlError(f"Zhihu API returned HTTP {response.status}.")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SourceFormatChanged("Zhihu API returned non-JSON content.") from exc
        if not isinstance(payload, dict):
            raise SourceFormatChanged("Zhihu API returned a non-object JSON payload.")
        if "error" in payload:
            reason = blocked_reason_from_text(json.dumps(payload["error"], ensure_ascii=False))
            if reason:
                raise CrawlBlocked(f"Zhihu API returned {reason}.")
            raise CrawlError("Zhihu API returned an error object.")
        return payload

    def collect_links(self, page: Any, token: str, *, kind: ContentKind, limit: int | None) -> list[str]:
        url = f"{ZH_DOMAIN}/people/{token}/{profile_path_for_kind(kind)}"
        self._progress(f"open {url}")
        self._goto(page, url)
        self._settle_page(page)

        links: list[str] = []
        seen: set[str] = set()
        last_height = 0
        stable_count = 0

        for _ in range(self.max_scrolls):
            for href in self._page_links(page):
                normalized = normalize_zhihu_link(href)
                if normalized and link_matches_kind(normalized, kind) and normalized not in seen:
                    seen.add(normalized)
                    links.append(normalized)
                    if limit is not None and len(links) >= limit:
                        return links
            height = int(page.evaluate("document.body.scrollHeight"))
            if height == last_height:
                stable_count += 1
                if stable_count >= self.stable_rounds:
                    break
            else:
                stable_count = 0
                last_height = height
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            self._sleep()
        return links

    def extract_item(self, page: Any, url: str, *, kind: ContentKind, author_token: str) -> ContentItem:
        self._progress(f"open item {url}")
        self._goto(page, url)
        self._settle_page(page)

        item = self._extract_from_initial_data(
            page.content(),
            url=url,
            kind=kind,
            author_token=author_token,
        )
        if item is not None and item.content_text:
            return item

        return self._extract_from_dom(page, url=url, kind=kind, author_token=author_token)

    def _extract_from_initial_data(
        self,
        html: str,
        *,
        url: str,
        kind: ContentKind,
        author_token: str,
    ) -> ContentItem | None:
        try:
            items = extract_items_from_initial_data(html, author_token=author_token, kinds=(kind,))
        except CrawlError:
            return None
        content_id = content_id_from_url(url, kind)
        if content_id:
            for item in items:
                if item.id == content_id and item.author_token == author_token:
                    return item
        return None

    def _extract_from_dom(self, page: Any, url: str, *, kind: ContentKind, author_token: str) -> ContentItem:
        # Profile pages can include recommendation links. Without the entity
        # payload, require visible author attribution rather than inventing it.
        actual_author = None
        for selector in (".AnswerItem .AuthorInfo a[href*='/people/']", ".Post-Header .AuthorInfo a[href*='/people/']", ".PinItem .AuthorInfo a[href*='/people/']", ".AuthorInfo a[href*='/people/']"):
            try:
                href = page.locator(selector).first.get_attribute("href", timeout=2_000)
                if href:
                    actual_author = parse_user_token(f"{ZH_DOMAIN}{href}" if href.startswith("/") else href)
                    break
            except Exception:
                continue
        if actual_author != author_token:
            raise SourceFormatChanged("DOM source author could not be verified.")
        item_id = content_id_from_url(url, kind) or stable_id_from_url(url)
        title = first_text(page, title_selectors_for_kind(kind)) or f"Zhihu {kind} {item_id}"
        content_html = first_inner_html(page, content_selectors_for_kind(kind))
        if not content_html:
            raise SourceFormatChanged(f"Could not extract content from {url}")
        metadata: dict[str, Any] = {}
        question_id = question_id_from_answer_url(url)
        if question_id:
            metadata["question_id"] = question_id
        return ContentItem(
            source="zhihu",
            kind=kind,
            id=item_id,
            title=title or f"Zhihu {kind} {item_id}",
            url=url,
            author_token=author_token,
            content_html=content_html,
            content_text=html_to_text(content_html),
            fetched_at=utc_now_iso(),
            metadata=metadata,
        )

    def _playwright_context(self) -> Any:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise CrawlError("Playwright is not installed. Run `pip install -e .`.") from exc
        return _PlaywrightContextManager(
            sync_playwright=sync_playwright,
            headless=self.headless,
            storage_state=self.storage_state,
        )

    def _settle_page(self, page: Any) -> None:
        self._sleep()
        try:
            page.wait_for_load_state("networkidle", timeout=8_000)
        except Exception:
            pass
        text = page.locator("body").inner_text(timeout=5_000)
        reason = blocked_reason_from_text(text) if len(text) < 1500 else None
        if reason:
            raise CrawlBlocked(f"Zhihu returned {reason} page.")

    def _goto(self, page: Any, url: str) -> None:
        for attempt in range(2):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                return
            except Exception:
                if attempt == 0:
                    self._sleep()
        raise CrawlError("Browser page navigation failed after bounded retries.") from None

    def _page_links(self, page: Any) -> list[str]:
        return page.eval_on_selector_all(
            ZH_LINK_SELECTOR,
            "elements => elements.map(e => e.href).filter(Boolean)",
        )

    def _sleep(self) -> None:
        if self.delay_seconds > 0:
            time.sleep(self.delay_seconds)

    def _progress(self, message: str) -> None:
        if self.progress is not None:
            self.progress(message)


class _PlaywrightContextManager:
    def __init__(self, *, sync_playwright: Any, headless: bool, storage_state: Path | None) -> None:
        self.sync_playwright = sync_playwright
        self.headless = headless
        self.storage_state = storage_state
        self.playwright: Any = None
        self.browser: Any = None
        self.context: Any = None

    def __enter__(self) -> Any:
        self.playwright = self.sync_playwright().start()
        try:
            self.browser = launch_chromium(self.playwright, headless=self.headless)
            options: dict[str, Any] = {"viewport": {"width": 1920, "height": 1080}}
            if self.storage_state:
                options["storage_state"] = load_storage_state(self.storage_state)
            self.context = self.browser.new_context(**options)
            return self.context
        except Exception:
            if self.browser is not None:
                self.browser.close()
            self.playwright.stop()
            raise

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.context is not None:
            self.context.close()
        if self.browser is not None:
            self.browser.close()
        if self.playwright is not None:
            self.playwright.stop()


def launch_chromium(playwright: Any, *, headless: bool) -> Any:
    """Use Playwright Chromium, then installed Chrome/Edge when it is absent."""

    try:
        return playwright.chromium.launch(headless=headless)
    except Exception as exc:
        if "Executable doesn't exist" not in str(exc):
            raise
        for channel in ("chrome", "msedge"):
            try:
                return playwright.chromium.launch(channel=channel, headless=headless)
            except Exception:
                continue
        raise CrawlError(
            "No usable Chromium browser was found. Install Chrome/Edge or run `playwright install chromium`. "
        ) from None


def save_zhihu_session(
    storage_state: Path,
    *,
    start_url: str = ZH_SIGNIN_URL,
    timeout_seconds: float = 300.0,
    poll_seconds: float = 2.0,
) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise CrawlError("Playwright is not installed. Run `pip install -e .`.") from exc

    storage_state.parent.mkdir(parents=True, exist_ok=True)
    playwright = sync_playwright().start()
    browser = None
    context = None
    try:
        browser = launch_chromium(playwright, headless=False)
        context = browser.new_context(viewport={"width": 1920, "height": 1080})
        page = context.new_page()
        page.goto(start_url, wait_until="domcontentloaded", timeout=60_000)
        print("A Chromium window is open. Log in to Zhihu there.")
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            cookies = context.cookies([ZH_DOMAIN])
            if has_zhihu_login_cookie(cookies):
                context.storage_state(path=str(storage_state))
                return
            time.sleep(poll_seconds)
        raise CrawlError("Timed out waiting for Zhihu login.")
    finally:
        if context is not None:
            context.close()
        if browser is not None:
            browser.close()
        playwright.stop()


def has_zhihu_login_cookie(cookies: Iterable[dict[str, Any]]) -> bool:
    for cookie in cookies:
        name = cookie.get("name")
        domain = str(cookie.get("domain") or "")
        value = cookie.get("value")
        if name == "z_c0" and value and domain.lstrip(".") in {"zhihu.com", "www.zhihu.com"}:
            return True
    return False


def load_storage_state(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise CrawlError("Local browser state could not be read; run login again.") from None
    if isinstance(payload, dict) and isinstance(payload.get("cookies"), list):
        return payload
    if isinstance(payload, list):
        return {"cookies": payload, "origins": []}
    raise CrawlError("Local browser state must contain a cookies list.")


def normalize_zhihu_link(href: str) -> str | None:
    parsed = urlparse(href)
    if not parsed.netloc and parsed.path.startswith("/"):
        parsed = urlparse(f"{ZH_DOMAIN}{href}")
    if parsed.netloc not in {"www.zhihu.com", "zhuanlan.zhihu.com"} or parsed.scheme not in {"http", "https", ""}:
        return None
    if not parsed.scheme:
        parsed = parsed._replace(scheme="https")
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))


def link_matches_kind(url: str, kind: ContentKind) -> bool:
    parsed = urlparse(url)
    if kind == "answer":
        return bool(re.search(r"/question/\d+/answer/\d+$", parsed.path))
    if kind == "article":
        return parsed.netloc == "zhuanlan.zhihu.com" and bool(re.search(r"/p/\d+$", parsed.path))
    if kind == "pin":
        return parsed.netloc == "www.zhihu.com" and bool(re.search(r"/pins?/\d+$", parsed.path))
    return False


def content_id_from_url(url: str, kind: ContentKind) -> str | None:
    if kind == "answer":
        pattern = r"/answer/(\d+)"
    elif kind == "article":
        pattern = r"/p/(\d+)"
    elif kind == "pin":
        pattern = r"/pins?/(\d+)"
    else:
        return None
    match = re.search(pattern, urlparse(url).path)
    return match.group(1) if match else None


def question_id_from_answer_url(url: str) -> str | None:
    match = re.search(r"/question/(\d+)/answer/\d+", urlparse(url).path)
    return match.group(1) if match else None


def stable_id_from_url(url: str) -> str:
    return re.sub(r"\W+", "-", urlparse(url).path).strip("-") or "unknown"


def first_inner_html(page: Any, selectors: Iterable[str]) -> str:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() > 0:
                html = locator.inner_html(timeout=2_000).strip()
                if html:
                    return html
        except Exception:
            continue
    return ""


def first_text(page: Any, selectors: Iterable[str]) -> str:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() > 0:
                text = locator.inner_text(timeout=2_000).strip()
                if text:
                    return text
        except Exception:
            continue
    return ""


def title_selectors_for_kind(kind: ContentKind) -> list[str]:
    if kind == "answer":
        return ANSWER_TITLE_SELECTORS
    if kind == "article":
        return ARTICLE_TITLE_SELECTORS
    if kind == "pin":
        return PIN_TITLE_SELECTORS
    raise ValueError(f"Unsupported Zhihu content kind: {kind}")


def content_selectors_for_kind(kind: ContentKind) -> list[str]:
    if kind == "answer":
        return ANSWER_CONTENT_SELECTORS
    if kind == "article":
        return ARTICLE_CONTENT_SELECTORS
    if kind == "pin":
        return PIN_CONTENT_SELECTORS
    raise ValueError(f"Unsupported Zhihu content kind: {kind}")


def api_url_for_kind(token: str, kind: ContentKind) -> str:
    if kind == "answer":
        return ANSWER_API.format(token=token)
    if kind == "article":
        return ARTICLE_API.format(token=token)
    if kind == "pin":
        return PIN_API.format(token=token)
    raise ValueError(f"Unsupported Zhihu content kind: {kind}")


def api_params_for_kind(kind: ContentKind, *, limit: int | None) -> dict[str, Any]:
    page_limit = min(20, limit) if limit is not None else 20
    if kind == "answer":
        return {"include": ANSWER_INCLUDE, "offset": 0, "limit": page_limit, "sort_by": "created"}
    if kind == "article":
        return {"include": ARTICLE_INCLUDE, "offset": 0, "limit": page_limit, "sort_by": "created"}
    if kind == "pin":
        return {"include": PIN_INCLUDE, "offset": 0, "limit": page_limit}
    raise ValueError(f"Unsupported Zhihu content kind: {kind}")
