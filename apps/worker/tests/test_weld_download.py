"""Tests for `_fetch_document` — fetching one recorder document, with the retry
and disclaimer-cookie handling that survives the site's intermittent failures.

`_download_documents` itself just wraps this in a browser, a login and a bounded
`asyncio.gather`, so the logic worth pinning lives here.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from survey_art.scrapers import weld_county
from survey_art.scrapers.weld_county import _DocRecord, _fetch_document

PDF = b"%PDF-1.4 body"


@pytest.fixture(autouse=True)
def _no_pacing(monkeypatch):
    """The real 2s-per-attempt pacing would make these tests take a minute."""
    monkeypatch.setattr(weld_county, "_DOC_FETCH_PAUSE_S", 0.0)


class _FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    async def body(self) -> bytes:
        return self._body


class _FakePage:
    def __init__(self, ctx: _FakeContext) -> None:
        self._ctx = ctx
        self.closed = False

    async def goto(self, url, **kwargs):
        self._ctx.goto_urls.append(url)

    async def wait_for_selector(self, selector, **kwargs):
        if self._ctx.next_href() is None:
            raise TimeoutError(selector)

    async def evaluate(self, _js):
        return self._ctx.next_href()

    async def query_selector(self, _selector):
        return None

    async def close(self):
        self.closed = True


class _FakeContext:
    """Serves a scripted sequence of `#printCustom` hrefs, one per attempt.

    A `None` entry models the failure this retry loop exists for: the viewer
    renders with no print button, which never means the document is missing.
    """

    def __init__(self, hrefs: list[str | None], status: int = 200, body: bytes = PDF) -> None:
        self._hrefs = list(hrefs)
        self._attempt = -1
        self.goto_urls: list[str] = []
        self.cookie_ops: list[str] = []
        self.pages: list[_FakePage] = []
        self.request = self
        self._status = status
        self._body = body

    def next_href(self):
        return self._hrefs[min(self._attempt, len(self._hrefs) - 1)]

    async def new_page(self):
        self._attempt += 1
        page = _FakePage(self)
        self.pages.append(page)
        return page

    async def get(self, _url):
        return _FakeResponse(self._status, self._body)

    async def clear_cookies(self, **_kwargs):
        self.cookie_ops.append("clear")

    async def add_cookies(self, _cookies):
        self.cookie_ops.append("add")


def _doc(reception: str = "1766550") -> _DocRecord:
    return _DocRecord(
        reception=reception,
        rec_date="",
        doc_type="EASEMENT",
        grantor="",
        grantee="",
        url=f"https://recording.weld.gov/web/web/integration/document/{reception}",
    )


async def _fetch(ctx, tmp_path: Path, doc: _DocRecord | None = None):
    return await _fetch_document(ctx, "exception", doc or _doc(), tmp_path, asyncio.Lock())


@pytest.mark.asyncio
async def test_saves_the_pdf_and_names_it_by_role_and_reception(tmp_path: Path):
    ctx = _FakeContext(["/web/document-image-pdf/x-1.pdf"])

    role, doc, paths = await _fetch(ctx, tmp_path)

    assert (role, doc.reception) == ("exception", "1766550")
    assert paths == [tmp_path / "exception_1766550.pdf"]
    assert paths[0].read_bytes() == PDF
    # A first attempt that works never touches the shared cookie.
    assert ctx.cookie_ops == []


@pytest.mark.asyncio
async def test_retries_a_missing_print_button_and_re_asserts_the_cookie(tmp_path: Path):
    ctx = _FakeContext([None, None, "/web/document-image-pdf/x-1.pdf"])

    _role, _doc, paths = await _fetch(ctx, tmp_path)

    assert paths == [tmp_path / "exception_1766550.pdf"]
    # Cookie is cleared and re-added together, once per retry (not on attempt 1).
    assert ctx.cookie_ops == ["clear", "add", "clear", "add"]
    assert all(p.closed for p in ctx.pages), "every attempt's page must be closed"


@pytest.mark.asyncio
async def test_gives_up_after_the_attempt_budget_without_raising(tmp_path: Path):
    ctx = _FakeContext([None])

    _role, _doc, paths = await _fetch(ctx, tmp_path)

    # An empty path list is the failure mode — a document that can't be fetched
    # must not abort the other documents in the same run.
    assert paths == []
    assert len(ctx.pages) == weld_county._DOC_FETCH_ATTEMPTS


@pytest.mark.asyncio
async def test_a_non_pdf_body_is_rejected_rather_than_saved(tmp_path: Path):
    """The print endpoint answers 200 with an HTML disclaimer page when the
    cookie didn't take — saving that as a .pdf would look like success."""
    ctx = _FakeContext(["/web/document-image-pdf/x-1.pdf"], body=b"<html>disclaimer")

    _role, _doc, paths = await _fetch(ctx, tmp_path)

    assert paths == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_a_broken_page_never_escapes_to_cancel_its_siblings(tmp_path: Path):
    """These run under one `asyncio.gather`, so a raise would cancel every other
    document in flight and skip the browser teardown."""

    class _BrokenContext(_FakeContext):
        async def new_page(self):
            raise RuntimeError("target closed")

    _role, _doc, paths = await _fetch(_BrokenContext([None]), tmp_path)

    assert paths == []


@pytest.mark.asyncio
async def test_concurrent_fetches_serialise_the_cookie_re_assert(tmp_path: Path):
    """Cookies are context-wide, so a sibling fetch must never observe the window
    between `clear_cookies` and `add_cookies` — otherwise it gets served the
    disclaimer instead of its document."""
    lock = asyncio.Lock()
    inside = 0

    class _RacyContext(_FakeContext):
        async def clear_cookies(self, **kwargs):
            nonlocal inside
            inside += 1
            assert inside == 1, "two fetches re-asserted the cookie at once"
            await asyncio.sleep(0)  # yield, so an unlocked version would interleave
            await super().clear_cookies(**kwargs)

        async def add_cookies(self, cookies):
            nonlocal inside
            await super().add_cookies(cookies)
            inside -= 1

    contexts = [_RacyContext([None, "/web/document-image-pdf/x-1.pdf"]) for _ in range(4)]
    results = await asyncio.gather(
        *(
            _fetch_document(ctx, "exception", _doc(str(i)), tmp_path, lock)
            for i, ctx in enumerate(contexts)
        )
    )

    # gather preserves order, which is what lets `_download_documents` line its
    # results up with the targets it was given.
    assert [doc.reception for _role, doc, _paths in results] == ["0", "1", "2", "3"]
    assert all(paths for _role, _doc, paths in results)
