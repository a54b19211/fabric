"""对外请求和一次读取的额度。

- get(url)：先等这个站点的限速间隔（fetch.rate），再发 GET；超时和 429 有界重试，每次重发前重新等限速；
  单个响应超过 cap 字节就放弃（防内存）。返回普通的 httpx.Response（正文已读完）。
- wait(url)：用浏览器或 scrapling 打开页面之前调一次：只等限速、记一个请求。site_only(url) 是开浏览器时加的参数：
  页面上只有这个站点自己的请求发得出去。
- Budget：读取插件自己建一个、在自己的循环里调：存一条之前 take_record（读满了就停），要发请求之前
  check（到截止时间就停）；增量读取每读到一条父记录问一次 known（连续 known_stop 条已存且没变就停）。
- SENT：这个进程发了几个请求、读到多少字节。read_public 每次读取前清零，读完写进读取结果。
  一个进程同一时间只跑一次读取（RQ worker 一次一个作业），所以放在模块里就够了。
"""

from __future__ import annotations

import time
from urllib.parse import urlparse

import httpx

from fetch import rate

KNOWN = "reached_known"  # stop_reason：增量读取读到了已存且没变的原件
SENT = {"requests": 0, "bytes": 0}


class Stop(BaseException):
    """读满了、到点了、读到旧的了：reason 就是 stop_reason，已读到的保留。它是 BaseException，
    插件里 except Exception 的地方吞不掉它。"""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class Budget:
    """一次读取的额度：最多存 max_records 条、deadline_seconds 秒后截止、增量读取时连续 known_stop 条已存就停。"""

    def __init__(self, request):
        self.source_id = request.source_id
        self.max_records, self.records = request.max_records, 0
        self.deadline = time.monotonic() + request.deadline_seconds
        self.known_stop, self.known_run = request.known_stop, 0

    def check(self):
        if time.monotonic() >= self.deadline:
            raise Stop("deadline")

    def need_record(self):
        """还能再存一条吗：读满了就停；顺便查截止时间。"""
        self.check()
        if self.records >= self.max_records:
            raise Stop("record_budget")

    def take_record(self):
        self.need_record()
        self.records += 1

    def known(self, item) -> bool:
        """增量读取：item 是不是库里已有且没变的（存了也不会多出版本）。连续 known_stop 条都是就停（reached_known）。
        item=None 是这一条没读到：不算已存，连续数清零。"""
        if not self.known_stop:
            return False
        from store import history  # 在这里才导入：store.history 导入了读取插件

        known = item is not None and history.stored_unchanged(self.source_id, item)
        self.known_run = self.known_run + 1 if known else 0
        if self.known_run >= self.known_stop:
            raise Stop(KNOWN)
        return known


def wait(url):
    """发一个请求之前：等这个站点的限速间隔，记一个请求。"""
    rate.wait_for_url(url)
    SENT["requests"] += 1


def site_only(url, proxy=None) -> list[str]:
    """浏览器的启动参数（StealthyFetcher 的 extra_flags）：只解析这个站点自己的域名（url 的主机去掉开头的 www.，和它的
    子域），别的主机一律解析失败。页面上第三方的广告、统计脚本会拖住 load 事件：2026-11-13 实测 FiercePharma 的文章页不拦
    就 60 秒等不到 load（三次都超时），拦了 4 秒。经代理（NCCN）时域名由代理解析，这条不起作用，还会让浏览器连不上代理，
    所以不加。"""
    if proxy:
        return []
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    return [f"--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE {host}, EXCLUDE *.{host}"]


def get(url, params=None, *, client=None, headers=None, cap=8 * 1024 * 1024, timeout=20.0, tries=None,
        deadline=None) -> httpx.Response:
    """GET url。每次发之前等限速；超时或 429 最多发 tries 次（默认 1 + APP.TASK_RETRIES），429 之后先等
    APP.TASK_RETRY_DELAY 秒（每次翻倍）。deadline（time.monotonic() 的值）到了就不再发，抛 Stop("deadline")；
    429 之后等不到下一次了，就把这个 429 交回去。响应超过 cap 字节抛 ValueError("response_too_large")。
    client 不给就临时开一个（跟随重定向）。"""
    from settings import APP

    tries = tries or 1 + max(0, APP.TASK_RETRIES)
    delay = min(max(float(APP.TASK_RETRY_DELAY), 0.0), 60.0)
    own = client is None
    client = client or httpx.Client(follow_redirects=True)
    try:
        for attempt in range(1, tries + 1):
            rate.wait_for_url(url)
            if deadline is not None and time.monotonic() >= deadline:
                raise Stop("deadline")
            SENT["requests"] += 1
            try:
                response = _read(client, url, params, headers, cap, timeout)
            except httpx.TimeoutException:
                if attempt == tries:
                    raise
                continue  # 超时多半是这条连接没通（个别 CDN 节点会卡住）：换一条马上重发，间隔由限速保证
            late = deadline is not None and time.monotonic() + delay >= deadline
            if response.status_code != 429 or attempt == tries or late:
                return response
            time.sleep(delay)
            delay = min(delay * 2, 60.0)
    finally:
        if own:
            client.close()


def _read(client, url, params, headers, cap, timeout) -> httpx.Response:
    """发一次，边读边数字节，超过 cap 就放弃。读到的是解压后的正文，所以交回的响应去掉 content-encoding。"""
    body = bytearray()
    with client.stream("GET", url, params=params, headers=headers, timeout=timeout) as response:
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > cap:
            raise ValueError("response_too_large")
        for chunk in response.iter_bytes():
            body.extend(chunk)
            SENT["bytes"] += len(chunk)
            if len(body) > cap:
                raise ValueError("response_too_large")
    kept = [(key, value) for key, value in response.headers.multi_items() if key.lower() != "content-encoding"]
    return httpx.Response(response.status_code, headers=kept, content=bytes(body), request=response.request)
