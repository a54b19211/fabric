"""The chat calls of graph.extraction.llm_client (qwen_extract_structured without streaming, qwen_chat), pinned at
the HTTP boundary: the request each one sends (URL, key, JSON body, timeouts), how often it is sent and how long it
waits in between, what the caller gets back and what is logged. The fake provider answers inside httpx's own
transport, so no request leaves the process."""

import json
import tempfile
import time

import httpx
import pytest
from loguru import logger

from graph.extraction import llm_client as cac
from settings import APP

BASE, EXTRACTION_BASE = "http://llm.test/v1", "http://extraction.test/v1"
TIMEOUT = {"connect": 5.0, "read": 600.0, "write": 600.0, "pool": 600.0}
DEFAULT_SYSTEM = "你是专业的医药文档信息提取助手。"
BUSY = {"error": {"message": "busy", "type": "server_error"}}


def completion(content):
    return {"id": "c-1", "object": "chat.completion", "created": 0, "model": "served-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


class Provider:
    """Each request takes the next scripted outcome, (status, JSON value or text) or an httpx exception class, and
    is recorded as it arrived. Waits between requests are recorded instead of slept."""

    def __init__(self, monkeypatch, *outcomes):
        self.outcomes, self.requests, self.waits = list(outcomes), [], []
        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", self._handle)
        monkeypatch.setattr(time, "sleep", self.waits.append)

    def _handle(self, request):  # bound to this provider, so httpx passes only the request
        self.requests.append({"method": request.method, "url": str(request.url),
                              "authorization": request.headers.get("authorization"),
                              "body": json.loads(request.content), "timeout": request.extensions.get("timeout")})
        assert self.outcomes, "the provider was asked more often than scripted"
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, type):
            raise outcome("scripted", request=request)
        status, value = outcome
        content = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return httpx.Response(status, content=content.encode(), headers={"content-type": "application/json"})


def assert_waits(waits, count):
    # 0.5 s before the first resend and 1 s before the second; the SDK shortens each by up to a quarter (jitter)
    assert len(waits) == count
    assert all(0.75 * base <= wait <= base for wait, base in zip(waits, (0.5, 1.0)))


@pytest.fixture
def endpoints(monkeypatch):
    for name, value in {"LLM_API_KEY": "key-main", "LLM_API_BASE": BASE, "LLM_MODEL_NAME": "chat-model",
                        "EXTRACTION_LLM_API_KEY": "key-extract", "EXTRACTION_LLM_API_BASE": EXTRACTION_BASE,
                        "EXTRACTION_LLM_MODEL_NAME": "extract-model"}.items():
        monkeypatch.setattr(APP, name, value)


@pytest.fixture
def logs():
    lines = []
    sink = logger.add(lambda message: lines.append(message.strip()), format="{level} {message}", level="INFO")
    yield lines
    logger.remove(sink)


@pytest.fixture
def saved(monkeypatch):
    """What the extraction keeps of an answer that is not JSON (a named temporary file), kept in memory here."""
    files = []

    class File:
        name = "/scratch/answer.json"

        def write(self, text):
            files.append(text)

        def close(self):
            pass

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", lambda **_: File())
    return files


def extraction_request(base, key, model, system):
    return {"method": "POST", "url": f"{base}/chat/completions", "authorization": f"Bearer {key}",
            "body": {"model": model, "temperature": 0.1, "response_format": {"type": "json_object"},
                     "messages": [{"role": "system", "content": system}, {"role": "user", "content": "the prompt"}]},
            "timeout": TIMEOUT}


# ---- qwen_extract_structured ----------------------------------------------------------------------------------

@pytest.mark.parametrize("use_extraction_model, base, key, model",
                         [(False, BASE, "key-main", "chat-model"),
                          (True, EXTRACTION_BASE, "key-extract", "extract-model")])
def test_extraction_sends_one_json_request_and_returns_the_object(monkeypatch, endpoints, logs, use_extraction_model,
                                                                  base, key, model):
    provider = Provider(monkeypatch, (200, completion('{"drug": "X", "n": 1}')))
    result = cac.qwen_extract_structured.fn("the prompt", system_prompt="the system",
                                            use_extraction_model=use_extraction_model)
    assert result == {"drug": "X", "n": 1}
    assert provider.requests == [extraction_request(base, key, model, "the system")]
    assert provider.waits == []
    assert f"INFO LLM 提取成功，模型={model}，字段数=2" in logs


def test_extraction_default_system_prompt_and_fenced_answer(monkeypatch, endpoints):
    provider = Provider(monkeypatch, (200, completion('```json\n{"a": [1, 2]}\n```')))
    assert cac.qwen_extract_structured.fn("the prompt") == {"a": [1, 2]}
    system = provider.requests[0]["body"]["messages"][0]
    assert system["role"] == "system" and system["content"].startswith(DEFAULT_SYSTEM)


def test_extraction_answer_that_is_not_json_is_saved_and_gives_nothing(monkeypatch, endpoints, logs, saved):
    Provider(monkeypatch, (200, completion('{"a": 1,,}')))
    assert cac.qwen_extract_structured.fn("the prompt") == {}
    assert saved == ['{"a": 1,,}']
    assert any(line.startswith("ERROR LLM 返回内容非合法 JSON\nJSON 解析失败:") and line.endswith("/scratch/answer.json")
               for line in logs)


def test_extraction_answer_without_content_gives_nothing(monkeypatch, endpoints, logs):
    Provider(monkeypatch, (200, completion(None)))
    assert cac.qwen_extract_structured.fn("the prompt") == {}
    assert "ERROR LLM API 调用异常: 'NoneType' object has no attribute 'strip'" in logs


def test_extraction_is_resent_after_server_errors_and_rate_limits(monkeypatch, endpoints):
    provider = Provider(monkeypatch, (500, BUSY), (429, BUSY), (200, completion('{"a": 1}')))
    assert cac.qwen_extract_structured.fn("the prompt", system_prompt="the system") == {"a": 1}
    assert provider.requests == [extraction_request(BASE, "key-main", "chat-model", "the system")] * 3
    assert_waits(provider.waits, 2)


def test_extraction_gives_up_after_two_resends(monkeypatch, endpoints, logs):
    provider = Provider(monkeypatch, (503, BUSY), (503, BUSY), (503, BUSY))
    assert cac.qwen_extract_structured.fn("the prompt") == {}
    assert len(provider.requests) == 3
    assert_waits(provider.waits, 2)
    assert "ERROR LLM API 调用异常: Error code: 503 - {'error': {'message': 'busy', 'type': 'server_error'}}" in logs


@pytest.mark.parametrize("status, value, logged",
                         [(400, {"error": {"message": "bad"}}, "Error code: 400 - {'error': {'message': 'bad'}}"),
                          (404, "not here", "not here"),
                          (401, "", "Error code: 401")])
def test_extraction_is_not_resent_after_a_client_error(monkeypatch, endpoints, logs, status, value, logged):
    provider = Provider(monkeypatch, (status, value))
    assert cac.qwen_extract_structured.fn("the prompt") == {}
    assert len(provider.requests) == 1 and provider.waits == []
    assert f"ERROR LLM API 调用异常: {logged}" in logs


@pytest.mark.parametrize("failure, logged", [(httpx.ConnectError, "Connection error."),
                                             (httpx.ReadTimeout, "Request timed out.")])
def test_extraction_is_resent_after_transport_failures(monkeypatch, endpoints, logs, failure, logged):
    provider = Provider(monkeypatch, failure, failure, failure)
    assert cac.qwen_extract_structured.fn("the prompt") == {}
    assert len(provider.requests) == 3
    assert_waits(provider.waits, 2)
    assert f"ERROR LLM API 调用异常: {logged}" in logs


def test_extraction_without_a_key_sends_nothing(monkeypatch, endpoints, logs):
    monkeypatch.setattr(APP, "LLM_API_KEY", "")
    provider = Provider(monkeypatch)
    assert cac.qwen_extract_structured.fn("the prompt") == {}
    assert provider.requests == []
    assert "ERROR LLM API Key 未配置（LLM_API_KEY），跳过结构化提取" in logs


# ---- qwen_chat ------------------------------------------------------------------------------------------------

def chat_request(temperature, max_tokens):
    return {"method": "POST", "url": f"{BASE}/chat/completions", "authorization": "Bearer key-main",
            "body": {"model": "chat-model", "temperature": temperature, "max_tokens": max_tokens,
                     "messages": [{"role": "system", "content": "rank"}, {"role": "user", "content": "candidates"}]},
            "timeout": TIMEOUT}


def test_chat_sends_the_callers_settings_and_returns_the_text(monkeypatch, endpoints):
    provider = Provider(monkeypatch, (200, completion('{"dict_id": 3}')))
    assert cac.qwen_chat(system_prompt="rank", user_message="candidates", temperature=0.0,
                         max_tokens=300) == '{"dict_id": 3}'
    assert provider.requests == [chat_request(0.0, 300)]


def test_chat_defaults_and_answer_without_content(monkeypatch, endpoints):
    provider = Provider(monkeypatch, (200, completion(None)))
    assert cac.qwen_chat(system_prompt="rank", user_message="candidates") == ""
    assert provider.requests == [chat_request(0.0, 500)]


def test_chat_is_resent_after_a_server_error(monkeypatch, endpoints):
    provider = Provider(monkeypatch, (502, "bad gateway"), (200, completion("ok")))
    assert cac.qwen_chat(system_prompt="rank", user_message="candidates", temperature=0.3, max_tokens=10) == "ok"
    assert provider.requests == [chat_request(0.3, 10)] * 2
    assert_waits(provider.waits, 1)


@pytest.mark.parametrize("outcomes, logged",
                         [([(400, {"error": {"message": "bad"}})], "Error code: 400 - {'error': {'message': 'bad'}}"),
                          ([httpx.ConnectError] * 3, "Connection error.")])
def test_chat_failure_gives_empty_text(monkeypatch, endpoints, logs, outcomes, logged):
    Provider(monkeypatch, *outcomes)
    assert cac.qwen_chat(system_prompt="rank", user_message="candidates") == ""
    assert f"ERROR qwen_chat 调用失败: {logged}" in logs


def test_chat_without_a_key_sends_nothing(monkeypatch, endpoints, logs):
    monkeypatch.setattr(APP, "LLM_API_KEY", "")
    provider = Provider(monkeypatch)
    assert cac.qwen_chat(user_message="candidates") == ""
    assert provider.requests == [] and "ERROR LLM API Key 未配置" in logs


# ---- both calls -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("answer", [{"choices": []}, "<html>ok</html>"])
def test_an_answer_that_is_no_chat_completion_gives_nothing(monkeypatch, endpoints, answer):
    Provider(monkeypatch, (200, answer), (200, answer))
    assert cac.qwen_extract_structured.fn("the prompt") == {}
    assert cac.qwen_chat(user_message="candidates") == ""


def test_chat_calls_build_no_sdk_client(monkeypatch, endpoints):
    # their HTTP side is llm_transport; only streaming and embeddings still build an OpenAI client (ADR 0007)
    monkeypatch.setattr(cac, "OpenAI", None)
    provider = Provider(monkeypatch, (200, completion('{"a": 1}')), (200, completion("ok")))
    assert cac.qwen_extract_structured.fn("the prompt") == {"a": 1}
    assert cac.qwen_chat(user_message="candidates") == "ok"
    assert len(provider.requests) == 2
