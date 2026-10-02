"""Test doubles shared across the Weld scraper tests."""

from __future__ import annotations


class FakeSearchPage:
    """Stands in for the Playwright page driven by _run_advanced_search.

    `rows` is either a flat list (returned for every search) or a callable
    taking the filled-in form, so a test can vary results by date window.
    """

    def __init__(self, rows):
        self._rows = rows
        self.form: dict[str, str] = {}
        self.searches: list[dict[str, str]] = []
        self.clicks: list[str] = []
        self.url = "https://recording.weld.gov/web/search/DOCSEARCH524S12"

    async def goto(self, *a, **k):
        pass

    async def click(self, *a, **k):
        if a and a[0] == "button:has-text('Yes - Continue')":
            raise Exception("no dialog")
        self.clicks.append(a[0] if a else "")

    async def fill(self, selector, value, *a, **k):
        self.form[selector] = value

    async def wait_for_load_state(self, *a, **k):
        pass

    async def wait_for_timeout(self, *a, **k):
        pass

    async def evaluate(self, *a, **k):
        self.searches.append(dict(self.form))
        return self._rows(self.form) if callable(self._rows) else self._rows
