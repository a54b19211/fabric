"""WeKnora HTTP contract with synthetic receipts only; never contacts a service."""

import json

import httpx
import pytest

from biz_rules.history_models import canonical, sha256
from core_engine import weknora_index_client as module


PID = "a" * 64
TOKEN = "synthetic-private-token"
BASE = "http://weknora.invalid:8080"


class TrackedStream(httpx.SyncByteStream):
    def __init__(self, chunks, *, clock=None, step=0):
        self.chunks = chunks
        self.clock, self.step = clock, step
        self.reads = 0
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            self.reads += 1
            if self.clock is not None:
                self.clock[0] += self.step
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    def close(self):
        self.closed = True


@pytest.fixture
def clients(monkeypatch):
    monkeypatch.setenv("WEKNORA_KDD_URL", BASE)
    monkeypatch.setenv("WEKNORA_KDD_TOKEN", TOKEN)
    opened = []

    def create(handler):
        calls = []

        def respond(request):
            calls.append(request)
            return handler(request)

        transport = httpx.Client(transport=httpx.MockTransport(respond))
        client = module.WeKnoraIndexClient(client=transport)
        opened.append(transport)
        return client, calls, transport

    yield create
    for transport in opened:
        transport.close()


def test_fixed_paths_headers_bodies_and_bounded_socket_reads(clients):
    client, calls, transport = clients(lambda _: httpx.Response(200, json={"ack": True}))
    body = {"version_id": "b" * 64, "content": "synthetic original"}
    assert client.status(PID) == {"ack": True}
    assert client.publish(body) == {"ack": True}
    assert client.disable(PID, "revoked") == {"ack": True}
    assert [(r.method, str(r.url)) for r in calls] == [
        ("GET", BASE + "/api/v1/kdd/publications/" + PID),
        ("POST", BASE + "/api/v1/kdd/publications"),
        ("POST", BASE + "/api/v1/kdd/publications/" + PID + "/disable"),
    ]
    assert json.loads(calls[1].content) == body
    assert json.loads(calls[2].content) == {
        "reason": "Fabric current state: revoked", "state": "revoked",
    }
    for request in calls:
        assert request.headers["Authorization"] == "Bearer " + TOKEN
        assert request.headers["Accept-Encoding"] == "identity"
        assert TOKEN not in str(request.url)
        assert request.extensions["timeout"] == {
            "connect": 5.0, "read": 5.0, "write": 5.0, "pool": 5.0,
        }
    client.close()
    assert not transport.is_closed  # An injected client's lifetime belongs to the caller.


@pytest.mark.parametrize("status", [301, 302, 401, 403, 409, 429, 500, 503])
@pytest.mark.parametrize("operation", ["status", "publish", "disable"])
def test_errors_are_not_empty_receipts_or_retried_writes(clients, status, operation):
    stream = TrackedStream([b"private upstream traceback and token"])
    client, calls, _ = clients(lambda _: httpx.Response(
        status, headers={"Location": "https://other.invalid/private"}, stream=stream,
    ))
    with pytest.raises(module.IndexServiceError) as error:
        if operation == "status":
            client.status(PID)
        elif operation == "publish":
            client.publish({"content": "private submitted body"})
        else:
            client.disable(PID, "revoked")
    assert error.value.code == "index_http_" + str(status)
    assert error.value.status == status
    assert len(calls) == 1
    assert stream.closed and stream.reads == 0
    for private in [TOKEN, "traceback", "private submitted body", "other.invalid"]:
        assert private not in str(error.value)


def test_only_status_allows_missing_and_does_not_read_error_body(clients):
    streams = []

    def response(_):
        stream = TrackedStream([b"not a JSON receipt"])
        streams.append(stream)
        return httpx.Response(404, stream=stream)

    client, calls, _ = clients(response)
    assert client.status(PID) is None
    for operation in [lambda: client.publish({}), lambda: client.disable(PID, "revoked")]:
        with pytest.raises(module.IndexServiceError) as error:
            operation()
        assert error.value.code == "index_http_404"
    assert len(calls) == 3
    assert all(stream.closed and stream.reads == 0 for stream in streams)


@pytest.mark.parametrize("exception,code,status", [
    (httpx.ConnectTimeout, "index_timeout", 504),
    (httpx.ReadTimeout, "index_timeout", 504),
    (httpx.WriteTimeout, "index_timeout", 504),
    (httpx.PoolTimeout, "index_timeout", 504),
    (httpx.ConnectError, "index_unavailable", 503),
    (httpx.ReadError, "index_unavailable", 503),
    (httpx.RemoteProtocolError, "index_unavailable", 503),
])
def test_transport_failures_are_sanitized_and_not_retried(clients, exception, code, status):
    def fail(request):
        raise exception("sensitive details: " + TOKEN, request=request)

    client, calls, _ = clients(fail)
    with pytest.raises(module.IndexServiceError) as error:
        client.publish({"content": "synthetic"})
    assert (error.value.code, error.value.status) == (code, status)
    assert TOKEN not in str(error.value) and "sensitive details" not in str(error.value)
    assert len(calls) == 1


def test_partial_response_failure_does_not_return_partial_success(clients):
    stream = TrackedStream([b'{"ack":', httpx.ReadError("private details " + TOKEN)])
    client, calls, _ = clients(lambda _: httpx.Response(200, stream=stream))
    with pytest.raises(module.IndexServiceError) as error:
        client.publish({})
    assert error.value.code == "index_unavailable"
    assert TOKEN not in str(error.value)
    assert len(calls) == 1 and stream.closed


def test_continuous_small_chunks_have_an_overall_deadline(clients, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    stream = TrackedStream([b"{", b'"ok":', b"true}"], clock=clock, step=7)
    client, calls, _ = clients(lambda _: httpx.Response(200, stream=stream))
    with pytest.raises(module.IndexServiceError) as error:
        client.publish({})
    assert error.value.code == "index_timeout" and error.value.status == 504
    assert len(calls) == 1 and stream.closed and stream.reads == 3


@pytest.mark.parametrize("status", [200, 404])
def test_headers_arriving_after_deadline_are_not_success_or_absence(clients, monkeypatch, status):
    clock = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    stream = TrackedStream([b"{}"])

    def delayed(_):
        clock[0] += 21
        return httpx.Response(status, stream=stream)

    client, calls, _ = clients(delayed)
    with pytest.raises(module.IndexServiceError) as error:
        client.status(PID)
    assert error.value.code == "index_timeout"
    assert len(calls) == 1 and stream.closed and stream.reads == 0


@pytest.mark.parametrize("extra", [0, 1])
def test_response_byte_limit_is_enforced_at_the_boundary(clients, extra):
    maximum = 4 * 1024 * 1024
    stream = TrackedStream([b"{}" + b" " * (maximum - 2), b" " * extra])
    client, calls, _ = clients(lambda _: httpx.Response(200, stream=stream))
    if extra:
        with pytest.raises(module.IndexServiceError) as error:
            client.status(PID)
        assert error.value.code == "index_response_budget"
    else:
        assert client.status(PID) == {}
    assert len(calls) == 1 and stream.closed


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "br"])
def test_unsupported_compression_is_rejected_before_body_decoding(clients, encoding):
    stream = TrackedStream([b"not even valid compressed data"])
    client, _, _ = clients(lambda _: httpx.Response(
        200, headers={"Content-Encoding": encoding}, stream=stream,
    ))
    with pytest.raises(module.IndexServiceError) as error:
        client.status(PID)
    assert error.value.code == "index_response_encoding"
    assert stream.closed and stream.reads == 0


@pytest.mark.parametrize("raw", [b"", b"[]", b"null", b'"string"', b'{"bad":', b"\xff"])
def test_non_object_or_invalid_json_never_becomes_a_receipt(clients, raw):
    stream = TrackedStream([raw])
    client, _, _ = clients(lambda _: httpx.Response(200, stream=stream))
    with pytest.raises(module.IndexServiceError) as error:
        client.status(PID)
    assert (error.value.code, error.value.status) == ("index_invalid_receipt", 502)
    assert stream.closed


@pytest.mark.parametrize("url,token", [
    ("", TOKEN), (BASE, ""), ("file:///tmp/service", TOKEN),
    ("ftp://weknora.invalid", TOKEN), ("http://user:secret@weknora.invalid", TOKEN),
])
def test_missing_config_or_embedded_credentials_fail_without_http(monkeypatch, url, token):
    monkeypatch.setenv("WEKNORA_KDD_URL", url)
    monkeypatch.setenv("WEKNORA_KDD_TOKEN", token)
    with pytest.raises(module.IndexServiceError) as error:
        module.WeKnoraIndexClient()
    assert (error.value.code, error.value.status) == ("index_not_configured", 503)
    assert "secret" not in str(error.value) and TOKEN not in str(error.value)


def test_owned_transport_does_not_follow_redirects_or_environment_proxy(monkeypatch):
    monkeypatch.setenv("WEKNORA_KDD_URL", BASE + "/")
    monkeypatch.setenv("WEKNORA_KDD_TOKEN", TOKEN)
    options = {}

    class Owned:
        closed = False

        def close(self):
            self.closed = True

    transport = Owned()

    def make_client(**kwargs):
        options.update(kwargs)
        return transport

    monkeypatch.setattr(module.httpx, "Client", make_client)
    client = module.WeKnoraIndexClient()
    assert client.base == BASE
    assert options["follow_redirects"] is False and options["trust_env"] is False
    client.close()
    assert transport.closed


def test_publication_id_uses_fixed_native_version_identity_not_mutable_content():
    manifest = {"knowledge_base_id": "synthetic-kb", "source_instance": "fixture",
                "source_id": "clinicaltrials", "source_record_id": "NCT00000001",
                "version_id": "b" * 64, "content_sha256": "c" * 64}
    expected = sha256(canonical([manifest[k] for k in [
        "knowledge_base_id", "source_instance", "source_id", "source_record_id", "version_id",
    ]]))
    assert module.publication_id(manifest) == expected
    assert module.publication_id({**manifest, "content_sha256": "d" * 64}) == expected
    assert module.publication_id({**manifest, "version_id": "e" * 64}) != expected
