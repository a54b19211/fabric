"""fetch.transport.get：每次发之前等限速；超时马上重发，429 先等一会儿再发；单个响应有大小上限；到点不再发。"""

import time

import httpx
import pytest

from fetch import rate, transport
from settings import APP

URL = "https://synthetic.invalid/x"


@pytest.fixture
def log(monkeypatch):
    """每次发之前的限速等待（记 "wait"，不真等）和 429 之后的睡眠（记秒数）。"""
    seen = []
    monkeypatch.setattr(rate, "wait", lambda group, interval: seen.append("wait"))
    monkeypatch.setattr(transport.time, "sleep", seen.append)
    monkeypatch.setattr(APP, "TASK_RETRIES", 2)
    monkeypatch.setattr(APP, "TASK_RETRY_DELAY", 10)
    return seen


def answering(*answers):
    answers = list(answers)

    def respond(request):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return httpx.Client(transport=httpx.MockTransport(respond))


def test_get_waits_before_every_send_resends_a_timeout_at_once_and_waits_after_a_429(log):
    client = answering(httpx.ConnectTimeout("t"), httpx.Response(429), httpx.Response(200, json={"ok": 1}))
    transport.SENT.update(requests=0, bytes=0)
    response = transport.get(URL, client=client)
    assert response.status_code == 200 and response.json() == {"ok": 1}
    assert log == ["wait", "wait", 10.0, "wait"]  # 超时马上重发；429 先等 10 秒
    assert transport.SENT["requests"] == 3


def test_get_hands_back_the_last_429_and_does_not_resend_other_errors(log):
    assert transport.get(URL, client=answering(*[httpx.Response(429)] * 3)).status_code == 429
    assert log == ["wait", 10.0, "wait", 20.0, "wait"]  # 3 次（1 + TASK_RETRIES），每次多等一倍
    log.clear()
    assert transport.get(URL, client=answering(httpx.Response(503))).status_code == 503 and log == ["wait"]


def test_get_refuses_a_response_over_its_cap_and_sends_nothing_after_the_deadline(log):
    streamed = httpx.Response(200, content=iter([b"a" * 400] * 5))  # 2000 字节，没有 content-length
    with pytest.raises(ValueError, match="response_too_large"):
        transport.get(URL, client=answering(streamed), cap=1000)
    with pytest.raises(transport.Stop) as stop:
        transport.get(URL, client=answering(), deadline=time.monotonic() - 1)
    assert stop.value.reason == "deadline"


def test_a_browser_resolves_only_the_site_it_opens():
    assert transport.site_only("https://www.nccn.org/guidelines/x") == [
        "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE nccn.org, EXCLUDE *.nccn.org"]
