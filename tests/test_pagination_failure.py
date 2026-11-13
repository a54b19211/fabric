"""JS pagination failures must reach the caller, using an in-memory browser."""

import ast
import concurrent.futures
import json
import queue
import re
import sys
import threading
import types
from abc import ABC, abstractmethod
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urljoin, urlparse

import pytest


class Quiet:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


def load_driver():
    source = Path(__file__).parents[1] / "fetch/pagination.py"
    tree = ast.parse(source.read_text())
    nodes = [n for n in tree.body if isinstance(n, ast.ClassDef)
             and n.name in {"BasePaginationDriver", "JsClickDriver"}]
    namespace = {
        "ABC": ABC, "abstractmethod": abstractmethod, "urlparse": urlparse,
        "urljoin": urljoin, "json": json, "concurrent": concurrent, "queue": queue, "threading": threading, "re": re,
        "transport": SimpleNamespace(wait=lambda url: None),
        "SITE_PROFILES": {}, "DEFAULT_HEADERS": {"User-Agent": "synthetic"},
        "logger": Quiet(), "time": SimpleNamespace(sleep=lambda _delay: None),
        "make_uuid": lambda value: value,
        "_rows_to_markdown": lambda rows: json.dumps(rows),
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), namespace)
    return namespace["JsClickDriver"]


class Element:
    def __init__(self, page, kind):
        self.page, self.kind = page, kind

    def get_attribute(self, name):
        return "/record-" + str(self.page.number) if name == "href" else ""

    def inner_text(self):
        return "row-" + str(self.page.number)

    def click(self):
        self.page.clicks += 1
        if self.page.advance:
            self.page.number += 1


class Page:
    url = "https://example.invalid/list"

    def __init__(self, advance):
        self.advance = advance
        self.number = 1
        self.clicks = 0
        self.waits = 0
        self.context_closes = 0
        self.browser_closes = 0

    def goto(self, *_args, **_kwargs):
        pass

    def title(self):
        return "SYNTHETIC"

    def query_selector_all(self, selector):
        return [Element(self, "table" if selector == "table" else "link")]

    def query_selector(self, selector):
        return None if selector == "li.is_active" else Element(self, "next")

    def wait_for_function(self, script, **_kwargs):
        self.waits += 1
        if not self.advance:
            raise TimeoutError("synthetic second page never changed")

    def wait_for_timeout(self, _delay):
        pass


def install_browser(monkeypatch, page):
    def close_context():
        page.context_closes += 1

    def close_browser():
        page.browser_closes += 1

    browser = SimpleNamespace(
        new_context=lambda **_kwargs: SimpleNamespace(new_page=lambda: page, close=close_context),
        close=close_browser,
    )

    @contextmanager
    def sync_playwright():
        yield SimpleNamespace(chromium=SimpleNamespace(launch=lambda **_kwargs: browser))

    package = types.ModuleType("playwright")
    api = types.ModuleType("playwright.sync_api")
    api.sync_playwright = sync_playwright
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", api)


def driver_for(monkeypatch, advance, pages=2):
    page = Page(advance)
    install_browser(monkeypatch, page)
    driver = load_driver()("site", {"max_pages": pages}, page.url)
    driver.site_cfg = {"pagination": {"next_btn_selector": ".next"}, "list_item_selector": "a"}
    driver._extract_table_rows = lambda _table: [["header"], ["row-" + str(page.number)]]
    return driver, page


def crawl(driver, method):
    """Run one crawl method to the end, keeping every page it hands over."""
    batches = []
    getattr(driver, method)(lambda batch: batches.append(batch) or True)
    return batches


@pytest.mark.parametrize("method", ["_playwright_crawl", "_playwright_crawl_tables"])
def test_second_page_timeout_cannot_return_a_duplicate_success(monkeypatch, method):
    driver, page = driver_for(monkeypatch, advance=False)
    with pytest.raises(RuntimeError, match="翻页"):
        crawl(driver, method)
    assert page.clicks == 1 and page.waits == 1
    assert page.context_closes == page.browser_closes == 1


@pytest.mark.parametrize("method", ["_playwright_crawl", "_playwright_crawl_tables"])
def test_bounded_success_does_not_request_an_unwanted_third_page(monkeypatch, method):
    driver, page = driver_for(monkeypatch, advance=True)
    batches = crawl(driver, method)
    assert len(batches) == 2
    assert batches[0] != batches[1]
    assert page.clicks == 1
    assert page.context_closes == page.browser_closes == 1


@pytest.mark.parametrize("method", ["_playwright_crawl", "_playwright_crawl_tables"])
def test_interruption_can_be_explicitly_restarted_from_first_page(monkeypatch, method):
    driver, _ = driver_for(monkeypatch, advance=False)
    with pytest.raises(RuntimeError):
        crawl(driver, method)
    recovered, _ = driver_for(monkeypatch, advance=True)
    assert len(crawl(recovered, method)) == 2


@pytest.mark.parametrize("method", ["_playwright_crawl", "_playwright_crawl_tables"])
def test_initial_read_failure_closes_browser_and_context(monkeypatch, method):
    driver, page = driver_for(monkeypatch, advance=True)

    def fail_goto(*_args, **_kwargs):
        raise TimeoutError("synthetic initial read timeout")

    page.goto = fail_goto
    with pytest.raises(TimeoutError, match="initial read"):
        crawl(driver, method)
    assert page.context_closes == page.browser_closes == 1


@pytest.mark.parametrize("method", ["_playwright_crawl", "_playwright_crawl_tables"])
def test_caller_that_stops_asking_stops_the_browser(monkeypatch, method):
    driver, page = driver_for(monkeypatch, advance=True, pages=200)
    assert getattr(driver, method)(lambda batch: False) is None  # first page handed over, caller wants no more
    assert page.clicks == 0
    assert page.context_closes == page.browser_closes == 1


@pytest.mark.parametrize(("method", "first"), [
    ("iter_detail_urls", "https://example.invalid/record-1"), ("iter_table_data", None)])
def test_one_page_is_handed_over_before_the_next_is_turned(monkeypatch, method, first):
    """configured for 200 pages: the first page comes out at once, the browser has not clicked, and closing the
    generator (the reader is full) closes the browser without ever asking for page two."""
    driver, page = driver_for(monkeypatch, advance=True, pages=200)
    pages = getattr(driver, method)()
    got = next(pages)
    assert (got == first) if first else (len(got) == 1)
    assert page.clicks == 0
    pages.close()
    assert page.clicks == 0 and page.context_closes == page.browser_closes == 1


def test_error_after_the_first_page_arrives_after_that_page(monkeypatch):
    driver, page = driver_for(monkeypatch, advance=False)
    pages = driver.iter_detail_urls()
    assert next(pages) == "https://example.invalid/record-1"
    with pytest.raises(RuntimeError, match="翻页"):
        next(pages)
    assert page.context_closes == page.browser_closes == 1


@pytest.mark.parametrize(("links", "expected"), [
    (
        ["/detail?id=1", "/detail?id=2", "/detail?id=1",
         "https://example.invalid/detail?id=2"],
        ["https://example.invalid/detail?id=1", "https://example.invalid/detail?id=2"],
    ),
    (
        ["/detail", "/detail", "https://example.invalid/detail"],
        ["https://example.invalid/detail"],
    ),
])
def test_detail_url_dedup_keeps_distinct_record_ids(links, expected):
    driver = load_driver()("site", {}, "https://example.invalid/list")
    driver.site_cfg = {"list_item_selector": "a"}
    page = SimpleNamespace(
        css=lambda _selector: [SimpleNamespace(attrib={"href": link}) for link in links],
    )
    assert driver._extract_detail_urls(page) == expected


def test_a_table_page_takes_its_date_from_the_newest_date_in_the_table(monkeypatch):
    """CDE's accepted-products tables have no publication date of their own: the newest "承办日期" in the table is the page's."""
    driver, page = driver_for(monkeypatch, advance=True)
    driver._extract_table_rows = lambda _table: [["受理号", "承办日期"], ["A1", "2026-11-10"], ["A2", "2026-11-12"], ["A3", ""]]
    [parsed] = driver._tables_on_page(page, 0)
    assert parsed["publish_date"] == "2026-11-12" == parsed["structured_data"]["publish_date"]
    driver._extract_table_rows = lambda _table: [["序号", "名称"], ["1", "x"]]  # a table with no date in it
    assert driver._tables_on_page(page, 0)[0]["publish_date"] == ""


def test_a_list_page_without_items_ends_the_walk_without_clicking_on(monkeypatch):
    driver, page = driver_for(monkeypatch, advance=True, pages=50)
    page.query_selector_all = lambda selector: []  # 第一页就没有条目
    assert crawl(driver, "_playwright_crawl") == [] and page.clicks == 0
    assert page.context_closes == page.browser_closes == 1


def test_the_walk_over_list_pages_stops_at_the_first_empty_one():
    base = load_driver().__mro__[1]

    class Link:
        attrib = {"href": "/a"}

    class Walk(base):
        def __init__(self):
            self.site_cfg, self.start_url, self.fetched = {"list_item_selector": "a"}, "https://x.invalid/list/", []

        def _iter_list_pages(self):
            for number in range(50):  # 配置翻 50 页
                self.fetched.append(number)
                yield SimpleNamespace(css=lambda selector, number=number: [Link()] if number == 0 else [])

    walk = Walk()
    assert list(walk.iter_detail_urls()) == ["https://x.invalid/a"]
    assert walk.fetched == [0, 1]  # 第二页是空的：到头了，不再去取后面的页
