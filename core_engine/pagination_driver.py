"""
翻页驱动层 —— 封装三种翻页策略，对外统一暴露 collect_detail_urls()。

策略说明：
  - UrlPatternDriver  : 翻页 URL 按规律生成（如 index.html / index_1.html）
  - CssNextBtnDriver  : 翻页靠找下一页按钮的 href（静态 HTML，无 JS）
  - JsClickDriver     : 翻页靠 Playwright 点击按钮（JS 动态渲染，覆盖基类）

新增翻页方式：继承 BasePaginationDriver，在 make_driver() 里注册即可。
"""

import time
import concurrent.futures
from abc import ABC, abstractmethod
from urllib.parse import urlparse, urljoin

from loguru import logger

from config.crawler_settings import SITE_PROFILES, DEFAULT_HEADERS
from core_engine.scrapling_client import fetch_page


# ===================================================================
#  基类
# ===================================================================

class BasePaginationDriver(ABC):
    """
    翻页采集基类，定义通用骨架：
      拿列表页 → 提取详情链接 → 判断下一页 → 循环
    """

    def __init__(self, site_key: str, profile: dict, start_url: str):
        self.site_key = site_key
        self.profile = profile
        self.start_url = start_url
        self.site_cfg = SITE_PROFILES.get(site_key, {})
        self.delay = profile.get("request_delay_seconds", 2.0)
        self.max_pages = profile.get("max_pages", 1)
        self.wait_sel = self.site_cfg.get("wait_selector")
        self.disable_resources = self.site_cfg.get("disable_resources", True)
        self.solve_cloudflare = self.site_cfg.get("solve_cloudflare", False)

    def iter_detail_urls(self):
        """
        模板方法：逐页 yield 详情页 URL，边翻页边产出，不等全部列表页采集完。
        JsClickDriver 会覆盖此方法，其余子类复用。
        """
        for page in self._iter_list_pages():
            urls = self._extract_detail_urls(page)
            logger.info(f"  提取到 {len(urls)} 条详情页链接")
            yield from urls

    @abstractmethod
    def _iter_list_pages(self):
        """子类实现：逐页 yield scrapling page 对象。"""

    def _extract_detail_urls(self, page) -> list[str]:
        """
        通用实现：用 list_item_selector 从列表页提取详情页链接。
        子类可覆盖以实现自定义提取逻辑。
        """
        selector = self.site_cfg.get("list_item_selector", "")
        if not selector:
            return []

        base_url = self.start_url
        parsed_base = urlparse(base_url)
        origin = f"{parsed_base.scheme}://{parsed_base.netloc}"

        urls = []
        for a in page.css(selector):
            href = a.attrib.get("href", "")
            if not href:
                continue
            if href.startswith("http"):
                urls.append(href)
            elif href.startswith("/"):
                urls.append(origin + href)
            else:
                urls.append(base_url.rsplit("/", 1)[0] + "/" + href)
        return urls

    def _fetch(self, url: str):
        """在独立线程里调用 fetch_page，避免 asyncio 冲突。"""
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(
                fetch_page.fn,
                url=url,
                delay=self.delay,
                wait_selector=self.wait_sel,
                disable_resources=self.disable_resources,
                solve_cloudflare=self.solve_cloudflare,
            ).result()


# ===================================================================
#  子类：url_pattern
# ===================================================================

class UrlPatternDriver(BasePaginationDriver):
    """
    翻页 URL 按规律生成，提前算出所有列表页 URL，逐一抓取。

    支持两种子模式（通过 pagination.subtype 区分）：
    - path（默认）: URL 路径拼接，如 index.html / index_1.html
      参数: first_page, other_pages（含 {n} 占位符）
    - query_param: query string 翻页，如 ?page=0 / ?page=1
      参数: param（参数名，默认 "page"），start（起始值，默认 0）
    """

    def _iter_list_pages(self):
        pagination = self.site_cfg.get("pagination", {})
        subtype = pagination.get("subtype", "path")

        if subtype == "query_param":
            param = pagination.get("param", "page")
            start = pagination.get("start", 0)
            urls = [
                f"{self.start_url}?{param}={start + n}"
                for n in range(self.max_pages)
            ]
        else:
            first_page = pagination.get("first_page", "index.html")
            other_pages = pagination.get("other_pages", "index_{n}.html")
            base = self.start_url.rsplit("/", 1)[0] + "/"
            urls = [base + first_page] + [
                base + other_pages.replace("{n}", str(n))
                for n in range(1, self.max_pages)
            ]

        for url in urls:
            logger.info(f"[列表页] {url}")
            page_data = self._fetch(url)
            yield page_data["page"]


# ===================================================================
#  子类：css_next_btn
# ===================================================================

class CssNextBtnDriver(BasePaginationDriver):
    """
    翻页靠找下一页按钮的 href，静态 HTML，无 JS 交互。
    参数来自 site_cfg["pagination"]: selector
    """

    def _iter_list_pages(self):
        pagination = self.site_cfg.get("pagination", {})
        next_selector = pagination.get("selector", "")
        current_url = self.start_url
        page_count = 0

        while current_url and page_count < self.max_pages:
            logger.info(f"[列表页 {page_count + 1}] {current_url}")
            page_data = self._fetch(current_url)
            page = page_data["page"]
            yield page

            next_btns = page.css(next_selector) if next_selector else []
            next_href = next_btns[0].attrib.get("href", "") if next_btns else ""
            current_url = urljoin(current_url, next_href) if next_href else None
            page_count += 1


# ===================================================================
#  子类：js_click
# ===================================================================

class JsClickDriver(BasePaginationDriver):
    """
    翻页靠 Playwright 点击按钮，JS 动态渲染。
    浏览器实例跨多个列表页复用，生命周期与前两种根本不同。

    iter_detail_urls 有两条路径：
    - 传 on_page_urls 回调：直接执行，每页翻完立刻在主线程触发回调
    - 不传：作为 generator 使用，收集完所有批次后逐条 yield
    注意：iter_detail_urls 是 generator 函数（含 yield from），
    必须被迭代才会执行，传回调时由 _run_with_callback 驱动执行。
    """

    def _iter_list_pages(self):
        raise NotImplementedError

    def iter_detail_urls(self, on_page_urls=None):
        # generator 路径：调用方迭代时执行
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            batches = executor.submit(self._playwright_crawl).result()
        for page_urls in batches:
            yield from page_urls

    def run_with_callback(self, on_page_urls):
        """直接执行 Playwright 爬取，每页完成后在主线程触发回调。"""
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            batches = executor.submit(self._playwright_crawl).result()
        for page_urls in batches:
            on_page_urls(page_urls)

    def _playwright_crawl(self) -> list[list[str]]:
        """在独立线程里运行 Playwright，返回按页分组的链接批次。"""
        from playwright.sync_api import sync_playwright

        pagination = self.site_cfg.get("pagination", {})
        next_btn_selector = pagination.get("next_btn_selector", "")
        item_link_selector = self.site_cfg.get("list_item_selector", "")
        parsed = urlparse(self.start_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        ua = DEFAULT_HEADERS["User-Agent"]
        batches: list[list[str]] = []

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            ctx = browser.new_context(user_agent=ua, locale="zh-CN")
            pw_page = ctx.new_page()
            pw_page.goto(self.start_url, wait_until="networkidle", timeout=30000)
            if self.wait_sel:
                pw_page.wait_for_selector(self.wait_sel, timeout=15000)

            page_count = 0
            while page_count < self.max_pages:
                logger.info(f"[列表页 {page_count + 1}] 当前URL: {pw_page.url}")

                hrefs = []
                for a in pw_page.query_selector_all(item_link_selector):
                    href = a.get_attribute("href") or ""
                    if href and href not in hrefs:
                        hrefs.append(href)

                page_urls = [(origin + h if h.startswith("/") else h) for h in hrefs]
                logger.info(f"  提取到 {len(page_urls)} 条详情页链接")
                batches.append(page_urls)

                next_btn = pw_page.query_selector(next_btn_selector)
                if not next_btn:
                    logger.info(f"没有下一页（next_btn_selector={next_btn_selector!r}），翻页结束")
                    break

                first_links = pw_page.query_selector_all(item_link_selector)
                old_first = first_links[0].get_attribute("href") if first_links else ""
                next_btn.click()

                if old_first:
                    safe_selector = item_link_selector.replace("'", "\\'")
                    safe_old = old_first.replace("'", "\\'")
                    try:
                        pw_page.wait_for_function(
                            f"""() => {{
                                const els = document.querySelectorAll('{safe_selector}');
                                return els.length > 0 && els[0].getAttribute('href') !== '{safe_old}';
                            }}""",
                            timeout=10000,
                        )
                    except Exception:
                        pw_page.wait_for_timeout(3000)
                else:
                    pw_page.wait_for_timeout(3000)

                page_count += 1
                time.sleep(self.delay)

            browser.close()
        return batches


# ===================================================================
#  工厂函数
# ===================================================================

_DRIVER_MAP = {
    "url_pattern": UrlPatternDriver,
    "css_next_btn": CssNextBtnDriver,
    "js_click": JsClickDriver,
}


def make_driver(site_key: str, profile: dict, start_url: str) -> BasePaginationDriver:
    """
    根据站点配置的 pagination.type 实例化对应的翻页驱动。
    新增翻页类型：继承 BasePaginationDriver，在 _DRIVER_MAP 里注册即可。
    """
    pagination_type = profile.get("pagination", {}).get("type", "url_pattern")
    driver_cls = _DRIVER_MAP.get(pagination_type)
    if driver_cls is None:
        raise ValueError(
            f"未知的翻页类型: {pagination_type!r}，"
            f"可用类型: {list(_DRIVER_MAP.keys())}"
        )
    return driver_cls(site_key, profile, start_url)
