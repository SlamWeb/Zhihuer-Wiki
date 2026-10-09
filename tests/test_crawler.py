from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

from zhihuer_wiki import crawler
from zhihuer_wiki.vendor.crawler.exceptions import CrawlBlocked, CrawlError, SourceFormatChanged
from zhihuer_wiki.vendor.crawler.markdown import render_item_markdown
from zhihuer_wiki.vendor.crawler.models import ContentItem
from zhihuer_wiki.vendor.crawler.zhihu import (
    ZhihuPublicCrawler, answer_payload_to_item, article_payload_to_item,
    extract_items_from_initial_data, fallback_profile, parse_user_token, pin_payload_to_item,
)
from zhihuer_wiki.vendor.crawler.zhihu_browser import (
    ZhihuBrowserCrawler, has_zhihu_login_cookie, launch_chromium,
    load_storage_state, normalize_zhihu_link,
)


def item(identifier="1", kind="answer", title="An original title", body="An exact source quote."):
    return ContentItem(
        source="zhihu", kind=kind, id=identifier, title=title,
        url=f"https://www.zhihu.com/question/10/answer/{identifier}" if kind == "answer" else f"https://zhuanlan.zhihu.com/p/{identifier}" if kind == "article" else f"https://www.zhihu.com/pin/{identifier}",
        author_token="alice", content_html=f"<p>{body}</p>", content_text=body,
        created_at="2026-01-01T00:00:00+00:00", updated_at="2026-01-02T00:00:00+00:00",
    )


def doubles(monkeypatch, public_scripts, browser_scripts=None):
    calls = []

    class Public:
        def __init__(self, **kwargs):
            self.outcomes = {}

        def crawl_profile(self, token):
            return fallback_profile(token)

        def iter_kind(self, token, *, kind, limit):
            calls.append(("public", kind, limit))
            script = public_scripts.get(kind, [])
            for value in script:
                if isinstance(value, Exception):
                    raise value
                yield value
            self.outcomes[kind] = "completed"

    class Browser(Public):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.storage_state = kwargs.get("storage_state")

        def iter_kind(self, token, *, kind, limit):
            calls.append(("browser", kind, limit, self.storage_state))
            for value in (browser_scripts or {}).get(kind, []):
                if isinstance(value, Exception):
                    raise value
                yield value
            self.outcomes[kind] = "completed"

    monkeypatch.setattr(crawler, "ZhihuPublicCrawler", Public)
    monkeypatch.setattr(crawler, "ZhihuBrowserCrawler", Browser)
    return calls


@pytest.mark.parametrize("value", ["alice", "@alice", "https://www.zhihu.com/people/alice", "https://zhihu.com/people/alice/answers/?utm=test#x"])
def test_profile_token_is_unambiguous(value):
    assert parse_user_token(value) == "alice"


@pytest.mark.parametrize("value", ["", "爱丽丝", "../alice", "alice/answers", "alice\\x", "alice?secret", "https://example.com/people/alice", "https://www.zhihu.com.evil.test/people/alice", "https://evil@www.zhihu.com/people/alice", "https://www.zhihu.com:443/people/alice", "https://www.zhihu.com/people/../alice", "https://www.zhihu.com/people/%2e%2e", "https://www.zhihu.com/people/alice/unknown", "file://www.zhihu.com/people/alice"])
def test_invalid_profiles_cannot_become_paths_or_external_requests(value):
    with pytest.raises(ValueError):
        parse_user_token(value)


@pytest.mark.parametrize("maximum", [0, -1, True, 1.5, "3"])
def test_invalid_maximum_fails_before_creating_output(tmp_path, maximum):
    with pytest.raises(ValueError):
        crawler.crawl("alice", tmp_path / "raw", max_items=maximum)
    assert not (tmp_path / "raw").exists()


def test_payload_and_markdown_preserve_quotes_links_and_dates():
    answer = answer_payload_to_item({"id": 42, "url": "http://www.zhihu.com/api/v4/answers/42", "question": {"id": 9, "title": "A question"}, "content": "<p>A precise quote.</p>", "created_time": 1760000000}, author_token="alice")
    article = article_payload_to_item({"id": 43, "title": "A title", "content": "<p>Article text</p>"}, author_token="alice")
    pin = pin_payload_to_item({"id": 44, "content": [{"type": "text", "content": "Pin text"}]}, author_token="alice")
    rendered = render_item_markdown(answer)
    assert 'url: "https://www.zhihu.com/question/9/answer/42"' in rendered
    assert 'created_at: "2025-10-09T08:53:20+00:00"' in rendered
    assert "A precise quote." in rendered
    assert article.url == "https://zhuanlan.zhihu.com/p/43"
    assert pin.content_text == "Pin text"


@pytest.mark.parametrize("converter", [answer_payload_to_item, article_payload_to_item])
@pytest.mark.parametrize("content", [None, "", "<p>   </p>"])
def test_excerpt_is_never_accepted_as_full_answer_or_article(converter, content):
    payload = {"id": 42, "title": "Title", "excerpt": "A truncated preview...", "content": content}
    with pytest.raises(SourceFormatChanged, match="full content"):
        converter(payload, author_token="alice")


@pytest.mark.parametrize("converter", [answer_payload_to_item, article_payload_to_item])
def test_full_content_html_is_valid_and_excerpt_stays_metadata(converter):
    parsed = converter({"id": 42, "content_html": "<p>The entire source.</p>", "excerpt": "A preview."}, author_token="alice")
    assert parsed.content_text == "The entire source."
    assert parsed.metadata["excerpt"] == "A preview."


def initial_data_html(kind, *, author=None, content="<p>Entire detail source.</p>", excerpt=None):
    source = {"id": 42, "title": "Source title", "content": content}
    if author is not None:
        source["author"] = author
    if excerpt is not None:
        source["excerpt"] = excerpt
    if kind == "answer":
        source["question"] = {"id": 10, "title": "Source title"}
    collection = {"answer": "answers", "article": "articles", "pin": "pins"}[kind]
    initial = {"initialState": {"entities": {collection: {"42": source}}}}
    return f'<script id="js-initialData" type="application/json">{json.dumps(initial)}</script>'


@pytest.mark.parametrize("kind", ["answer", "article", "pin"])
@pytest.mark.parametrize("author", [None, {}, {"url_token": "other"}, {"url_token": "alice"}])
def test_detail_initial_data_requires_explicit_matching_author(kind, author):
    html = initial_data_html(kind, author=author)
    url = item("42", kind).url
    extracted = extract_items_from_initial_data(html, author_token="alice", kinds=(kind,))
    detail = ZhihuBrowserCrawler(delay_seconds=0)._extract_from_initial_data(html, url=url, kind=kind, author_token="alice")
    if author == {"url_token": "alice"}:
        assert len(extracted) == 1
        assert detail is not None and detail.author_token == "alice"
    else:
        assert extracted == []
        assert detail is None


@pytest.mark.parametrize("kind", ["answer", "article"])
def test_attributed_detail_preview_also_requires_full_content(kind):
    html = initial_data_html(kind, author={"url_token": "alice"}, content=None, excerpt="A truncated preview...")
    with pytest.raises(SourceFormatChanged, match="full content"):
        extract_items_from_initial_data(html, author_token="alice", kinds=(kind,))
    assert ZhihuBrowserCrawler(delay_seconds=0)._extract_from_initial_data(html, url=item("42", kind).url, kind=kind, author_token="alice") is None


def test_missing_detail_author_continues_to_dom_author_verification(monkeypatch):
    client = ZhihuBrowserCrawler(delay_seconds=0)
    page = SimpleNamespace(content=lambda: initial_data_html("answer"), locator=lambda selector: SimpleNamespace(first=SimpleNamespace(get_attribute=lambda *args, **kwargs: None)))
    monkeypatch.setattr(client, "_goto", lambda *args: None)
    monkeypatch.setattr(client, "_settle_page", lambda *args: None)
    with pytest.raises(SourceFormatChanged, match="author"):
        client.extract_item(page, item("42").url, kind="answer", author_token="alice")


def test_fallback_is_per_kind_even_when_another_kind_succeeds(monkeypatch, tmp_path):
    calls = doubles(monkeypatch, {"answer": [item()], "article": [], "pin": [CrawlBlocked("blocked")]}, {"article": [item("2", "article")], "pin": [item("3", "pin")]})
    result = crawler.crawl("alice", tmp_path, delay_seconds=0)
    assert result["status"] == "completed"
    assert result["complete"] is True
    assert result["fetched_count"] == 3
    assert [value[:2] for value in calls] == [("public", "answer"), ("public", "article"), ("public", "pin"), ("browser", "article"), ("browser", "pin")]
    rows = [json.loads(line) for line in (tmp_path / "manifest.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {row["kind"] for row in rows} == {"answer", "article", "pin"}
    assert all((tmp_path / row["path"]).exists() for row in rows)


def test_global_smoke_cap_is_limited_and_skips_unvisited_kinds(monkeypatch, tmp_path):
    calls = doubles(monkeypatch, {"answer": [item(), item("2")]})
    result = crawler.crawl("alice", tmp_path, max_items=1, delay_seconds=0)
    assert result["status"] == "limited"
    assert result["complete"] is False
    assert result["fetched_count"] == 1
    assert calls == [("public", "answer", 1)]


def test_incremental_merge_preserves_unfetched_items_and_updates_same_id(monkeypatch, tmp_path):
    doubles(monkeypatch, {"answer": [item(), item("2", body="Keep this old source.")]})
    first = crawler.crawl("alice", tmp_path, kinds=("answer",), delay_seconds=0)
    assert first["status"] == "completed"
    rows = [json.loads(line) for line in (tmp_path / "manifest.jsonl").read_text(encoding="utf-8").splitlines()]
    old_path = rows[0]["path"]
    keep_path = rows[1]["path"]
    keep_bytes = (tmp_path / keep_path).read_bytes()
    doubles(monkeypatch, {"answer": [item(title="Changed title", body="Updated source."), CrawlError("page failed")]}, {"answer": [CrawlBlocked("blocked")]})
    second = crawler.crawl("alice", tmp_path, kinds=("answer",), delay_seconds=0)
    assert second["status"] == "partial"
    assert second["complete"] is False
    assert second["total_count"] == 2
    assert second["updated_count"] == 1
    assert "Updated source." in (tmp_path / old_path).read_text(encoding="utf-8")
    assert (tmp_path / keep_path).read_bytes() == keep_bytes
    assert json.loads((tmp_path / "crawl_checkpoint.json").read_text(encoding="utf-8"))["status"] == "partial"
    doubles(monkeypatch, {"answer": [item(), item("2"), item("3")]})
    resumed = crawler.crawl("alice", tmp_path, kinds=("answer",), delay_seconds=0)
    assert resumed["status"] == "completed"
    assert resumed["total_count"] == 3
    assert resumed["updated_count"] == 2


def test_checkpoint_survives_an_interrupted_generator(monkeypatch, tmp_path):
    class Interrupted:
        outcomes = {}

        def __init__(self, **kwargs):
            pass

        def crawl_profile(self, token):
            return fallback_profile(token)

        def iter_kind(self, token, **kwargs):
            yield item()
            raise KeyboardInterrupt()

    monkeypatch.setattr(crawler, "ZhihuPublicCrawler", Interrupted)
    with pytest.raises(KeyboardInterrupt):
        crawler.crawl("alice", tmp_path, kinds=("answer",), delay_seconds=0)
    checkpoint = json.loads((tmp_path / "crawl_checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["status"] == "running"
    assert checkpoint["fetched_count"] == 1
    doubles(monkeypatch, {"answer": [item(), item("2")]})
    assert crawler.crawl("alice", tmp_path, kinds=("answer",), delay_seconds=0)["total_count"] == 2


def test_no_source_items_is_failed_even_with_prior_data(monkeypatch, tmp_path):
    doubles(monkeypatch, {"answer": [item()]})
    crawler.crawl("alice", tmp_path, kinds=("answer",), delay_seconds=0)
    doubles(monkeypatch, {"answer": [CrawlBlocked("blocked")]}, {"answer": [CrawlBlocked("blocked")]})
    result = crawler.crawl("alice", tmp_path, kinds=("answer",), delay_seconds=0)
    assert result["status"] == "failed"
    assert result["fetched_count"] == 0
    assert result["total_count"] == 1
    assert result["complete"] is False


def test_credential_payload_and_path_never_appear_in_summary_or_logs(monkeypatch, tmp_path, capsys):
    secret = "private-cookie-value-do-not-log"
    state = tmp_path / "auth" / "private-state.json"
    state.parent.mkdir()
    state.write_text(json.dumps({"cookies": [{"name": "z_c0", "value": secret, "domain": ".zhihu.com"}], "origins": []}), encoding="utf-8")
    calls = doubles(monkeypatch, {"answer": [CrawlBlocked(secret)]}, {"answer": [RuntimeError(f"cookie={secret}; state={state}")]})
    root = tmp_path / "raw"
    result = crawler.crawl("alice", root, kinds=("answer",), storage_state=state, delay_seconds=0)
    artifacts = "\n".join(path.read_text(encoding="utf-8") for path in root.rglob("*") if path.is_file())
    assert secret not in artifacts + json.dumps(result) + capsys.readouterr().out
    assert str(state) not in artifacts + json.dumps(result)
    assert calls[-1][-1] == state
    assert result["status"] == "failed"
    with pytest.raises(ValueError, match="outside"):
        crawler.crawl("alice", root, storage_state=root / "state.json")


def test_manifest_paths_and_author_identity_fail_closed(monkeypatch, tmp_path):
    doubles(monkeypatch, {"answer": [item()]})
    (tmp_path / "manifest.jsonl").write_text(json.dumps({"kind": "answer", "id": "1", "author_token": "alice", "path": "../escape.md"}) + "\n", encoding="utf-8")
    with pytest.raises(CrawlError, match="relative"):
        crawler.crawl("alice", tmp_path)
    (tmp_path / "manifest.jsonl").unlink()
    wrong = item()
    wrong.author_token = "other"
    doubles(monkeypatch, {"answer": [wrong]}, {"answer": [CrawlBlocked("blocked")]})
    result = crawler.crawl("alice", tmp_path, kinds=("answer",), delay_seconds=0)
    assert result["status"] == "failed"
    assert not list(tmp_path.rglob("*.md"))


def test_public_pagination_proves_end_and_preserves_partial_items(monkeypatch):
    payload = {"data": [{"id": 1, "question": {"id": 10, "title": "Title"}, "content": "<p>Full text</p>"}], "paging": {"is_end": False, "next": "https://www.zhihu.com/api/v4/members/alice/answers?offset=1"}}
    client = ZhihuPublicCrawler(delay_seconds=0)
    pages = iter([payload, SourceFormatChanged("bad page")])

    def get(*args, **kwargs):
        value = next(pages)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(client, "_get_json", get)
    iterator = client.iter_kind("alice", kind="answer", limit=None)
    scoped_item = next(iterator)
    assert scoped_item.id == "1"
    assert scoped_item.author_token == "alice"  # Author-scoped API membership proves identity.
    with pytest.raises(SourceFormatChanged):
        next(iterator)
    assert client.outcomes["answer"] != "completed"
    monkeypatch.setattr(client, "_get_json", lambda *args, **kwargs: {**payload, "paging": {"is_end": True}})
    assert len(list(client.iter_kind("alice", kind="answer", limit=None))) == 1
    assert client.outcomes["answer"] == "completed"


def test_public_retries_are_bounded_and_response_errors_are_private(monkeypatch):
    from zhihuer_wiki.vendor.crawler import zhihu

    calls = []

    def unavailable(*args, **kwargs):
        calls.append(kwargs)
        raise URLError("a sensitive upstream error")

    monkeypatch.setattr(zhihu, "urlopen", unavailable)
    client = ZhihuPublicCrawler(delay_seconds=0)
    with pytest.raises(CrawlError, match="bounded") as exc:
        client._get_json("https://www.zhihu.com/api/v4/members/alice", referer="https://www.zhihu.com/people/alice")
    assert len(calls) == 3
    assert "sensitive" not in str(exc.value)
    calls.clear()

    def blocked(*args, **kwargs):
        calls.append(kwargs)
        raise HTTPError("https://www.zhihu.com", 403, "private-cookie-value", {}, None)

    monkeypatch.setattr(zhihu, "urlopen", blocked)
    with pytest.raises(CrawlBlocked) as exc:
        client._get_json("https://www.zhihu.com/api/v4/members/alice", referer="https://www.zhihu.com/people/alice")
    assert len(calls) == 1
    assert "private-cookie-value" not in str(exc.value)
    with pytest.raises(SourceFormatChanged):
        client._get_json("https://evil.test/api/v4/members/alice", referer="https://www.zhihu.com")


def test_browser_api_generator_yields_before_later_failure(monkeypatch):
    client = ZhihuBrowserCrawler(delay_seconds=0)
    payloads = iter([{"data": [{"id": 1, "content": "<p>Full answer</p>", "question": {"id": 10, "title": "Title"}}], "paging": {"is_end": False, "next": "https://www.zhihu.com/api/v4/members/alice/answers?offset=1"}}, CrawlBlocked("blocked")])

    def get(*args, **kwargs):
        value = next(payloads)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(client, "_api_get_json", get)
    iterator = client._crawl_api_items(object(), "alice", kind="answer", limit=None)
    scoped_item = next(iterator)
    assert scoped_item.content_text == "Full answer"
    assert scoped_item.author_token == "alice"
    with pytest.raises(CrawlBlocked):
        next(iterator)


def test_browser_fallback_uses_edge_if_chrome_and_bundle_unavailable():
    calls = []
    expected = object()

    def launch(**kwargs):
        calls.append(kwargs)
        if kwargs.get("channel") == "msedge":
            return expected
        raise RuntimeError("Executable doesn't exist")

    playwright = SimpleNamespace(chromium=SimpleNamespace(launch=launch))
    assert launch_chromium(playwright, headless=True) is expected
    assert [call.get("channel") for call in calls] == [None, "chrome", "msedge"]


def test_dom_fallback_rejects_unverified_or_recommended_author():
    page = SimpleNamespace(locator=lambda selector: SimpleNamespace(first=SimpleNamespace(get_attribute=lambda *args, **kwargs: "/people/other")))
    with pytest.raises(SourceFormatChanged, match="author"):
        ZhihuBrowserCrawler(delay_seconds=0)._extract_from_dom(page, "https://www.zhihu.com/question/10/answer/1", kind="answer", author_token="alice")


def test_browser_state_and_url_helpers(tmp_path):
    state = tmp_path / "state.json"
    state.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
    assert load_storage_state(state) == {"cookies": [], "origins": []}
    state.write_text('secret-not-json', encoding="utf-8")
    with pytest.raises(CrawlError) as exc:
        load_storage_state(state)
    assert "secret-not-json" not in str(exc.value)
    assert has_zhihu_login_cookie([{"name": "z_c0", "value": "value", "domain": ".zhihu.com"}])
    assert not has_zhihu_login_cookie([{"name": "z_c0", "value": "value", "domain": "evilzhihu.com"}])
    assert normalize_zhihu_link("javascript://www.zhihu.com/question/1/answer/2") is None


def test_login_is_explicit_and_returns_no_credentials(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(crawler, "save_zhihu_session", calls.append)
    result = crawler.login(tmp_path / "state.json")
    assert calls == [(tmp_path / "state.json").resolve()]
    assert result == {"status": "saved", "credentials": "local_only"}
