from __future__ import annotations

import asyncio
import copy
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web

sys.path.insert(0, str(Path(__file__).absolute().parents[1] / "scripts"))
import e2e_history_rpc_proxy as proxy


def control(tmp_path, mode="pass", targets=None):
    path = tmp_path / "control.json"
    path.write_text(
        json.dumps({"mode": mode, "targets": ["a", "b"] if targets is None else targets})
    )
    return path


def engine(tmp_path, mode="pass", targets=None):
    return proxy.HistoryProxy(
        checkpoint=100,
        control=control(tmp_path, mode, targets),
        events=tmp_path / "events.jsonl",
        hold_timeout=0.001,
    )


@pytest.mark.parametrize(
    "value",
    [
        {},
        [],
        None,
        {"mode": "unexpected", "targets": ["a"]},
        {"mode": "pass", "targets": []},
        {"mode": "hold", "targets": ["public"]},
        {"mode": "pass", "targets": ["a"], "extra": True},
    ],
)
def test_control_rejects_invalid_shape_and_modes(value):
    with pytest.raises(ValueError):
        proxy.settings_from_json(value)


def test_control_read_is_bounded(tmp_path):
    path = tmp_path / "large.json"
    path.write_bytes(b" " * 4097)
    with pytest.raises(ValueError, match="4096"):
        proxy.load_settings(path)


@pytest.mark.parametrize("raw,expected", [(None, None), ("0", None), (0, None), ("1", 1), (42, 42)])
def test_canonical_rpc_heights(raw, expected):
    assert proxy.rpc_height(raw) == expected


@pytest.mark.parametrize("raw", [True, False, -1, "-1", "01", "+1", 1.5, []])
def test_bad_rpc_heights(raw):
    with pytest.raises(ValueError):
        proxy.rpc_height(raw)


@pytest.mark.parametrize(
    "method,path,query,body",
    [
        ("DELETE", "/commit", {}, b""),
        ("GET", "/broadcast_tx_sync", {}, b""),
        ("POST", "/", {}, b"[]"),
        ("POST", "/", {}, b'{"method":[],"params":{}}'),
        ("POST", "/", {}, b'{"method":"validators","params":[]}'),
    ],
)
def test_only_single_read_only_rpc_calls_are_forwarded(method, path, query, body):
    with pytest.raises(ValueError):
        proxy.inspect_request(method, path, query, body)


def test_get_and_post_transport_keep_exact_historical_height():
    expected = proxy.RpcRead("commit", 99)
    assert proxy.inspect_request("GET", "/commit", {"height": "99"}, b"") == expected
    assert (
        proxy.inspect_request("POST", "/", {}, b'{"method":"commit","params":{"height":"99"}}')
        == expected
    )
    with pytest.raises(ValueError, match="large"):
        proxy.inspect_request("POST", "/", {}, b"x" * (proxy.MAX_REQUEST_BYTES + 1))


@pytest.mark.parametrize(
    "url",
    [
        "https://node-0:26657",
        "http://example.com",
        "http://user:password@node-0",
        "http://node-0/private",
        "http://node-0?query=x",
        "http://node-0#fragment",
    ],
)
def test_upstream_cannot_escape_private_review_sources(url):
    with pytest.raises(ValueError):
        proxy.upstream_url(url)


def test_local_upstream_origin_is_preserved():
    assert proxy.upstream_url("http://node-0:26657/") == "http://node-0:26657"
    assert proxy.upstream_url("http://127.0.0.1:12345") == "http://127.0.0.1:12345"


def test_fault_only_matches_strictly_precheckpoint_history():
    assert proxy.historical(proxy.RpcRead("commit", 99), 100)
    for request in [
        proxy.RpcRead("commit", 100),
        proxy.RpcRead("commit", None),
        proxy.RpcRead("status", 99),
    ]:
        assert not proxy.historical(request, 100)


def test_wrong_commit_changes_authentication_input_without_mutating_source():
    original = {
        "result": {
            "signed_header": {
                "header": {"app_hash": "0" * 64, "height": "99"},
                "commit": {"block_id": {"hash": "a" * 64}},
            }
        }
    }
    baseline = copy.deepcopy(original)
    changed = proxy.wrong_commit(original)
    assert original == baseline
    assert changed["result"]["signed_header"]["header"]["app_hash"] == "1" + "0" * 63
    assert (
        changed["result"]["signed_header"]["commit"]
        == original["result"]["signed_header"]["commit"]
    )
    with pytest.raises(ValueError):
        proxy.wrong_commit({"result": {"signed_header": {"header": {"app_hash": "short"}}}})


@pytest.mark.parametrize(
    "rpc_call",
    [proxy.RpcRead("commit", 100), proxy.RpcRead("commit", None), proxy.RpcRead("status", 99)],
)
def test_hold_does_not_delay_nonhistorical_requests(tmp_path, rpc_call):
    instance = engine(tmp_path, "hold")
    assert asyncio.run(instance.wait_for_release("a", rpc_call)).mode == "pass"
    assert not instance.events.exists()


def test_hold_release_to_honest_peer_is_pass_through(tmp_path, monkeypatch):
    instance = engine(tmp_path, "hold")

    async def release(_):
        control(tmp_path, "wrong_commit", ["b"])

    monkeypatch.setattr(proxy.asyncio, "sleep", release)
    assert asyncio.run(instance.wait_for_release("a", proxy.RpcRead("commit", 99))).mode == "pass"


def test_hold_can_be_explicitly_released_and_records_boundary(tmp_path, monkeypatch):
    instance = engine(tmp_path, "hold")

    async def release(_):
        control(tmp_path, "pass")

    monkeypatch.setattr(proxy.asyncio, "sleep", release)
    assert asyncio.run(instance.wait_for_release("a", proxy.RpcRead("commit", 99))).mode == "pass"
    event = json.loads(instance.events.read_text())
    assert event["kind"] == "history-held" and event["height"] == 99
    assert set(event) == {"schema_version", "kind", "peer", "method", "height", "at_ns"}


def test_hold_timeout_is_bounded(tmp_path):
    instance = engine(tmp_path, "hold")
    with pytest.raises(TimeoutError):
        asyncio.run(instance.wait_for_release("a", proxy.RpcRead("commit", 99)))


class Stream:
    def __init__(self, chunks):
        self.chunks = chunks
        self.read = 0

    async def iter_chunked(self, _):
        for chunk in self.chunks:
            self.read += 1
            yield chunk


def test_response_stream_is_complete_and_bounded(monkeypatch):
    assert asyncio.run(proxy.bounded_body(Stream([b"a", b"bc"]))) == b"abc"
    monkeypatch.setattr(proxy, "MAX_RESPONSE_BYTES", 10)
    stream = Stream([b"123456", b"123456", b"never read"])
    with pytest.raises(ValueError):
        asyncio.run(proxy.bounded_body(stream))
    assert stream.read == 2


def test_forward_preserves_read_transport_and_records_controlled_wrong_header(tmp_path):
    instance = engine(tmp_path, "wrong_commit")
    original = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"signed_header": {"header": {"app_hash": "a" * 64}, "commit": {"height": "99"}}},
    }
    observed = []

    class Session:
        @asynccontextmanager
        async def request(self, *args, **kwargs):
            observed.append((args, kwargs))
            yield SimpleNamespace(
                status=200,
                headers={"Content-Type": "application/json"},
                content=Stream([json.dumps(original).encode()]),
            )

    request = SimpleNamespace(
        method="GET",
        path="/commit",
        query={"height": "99"},
        rel_url="/commit?height=99",
        content_type="application/json",
        read=AsyncMock(return_value=b""),
    )
    result = asyncio.run(instance.handle(Session(), "a", "http://node-0:26657", request))
    assert result.status == 200
    assert json.loads(result.body) == proxy.wrong_commit(original)
    assert observed == [
        (
            ("GET", "http://node-0:26657/commit?height=99"),
            {"data": b"", "headers": {"Content-Type": "application/json"}},
        )
    ]
    assert json.loads(instance.events.read_text())["kind"] == "wrong-commit-sent"


@pytest.mark.parametrize("error,status", [(ValueError("bad"), 400), (TimeoutError("hold"), 504)])
def test_handler_failure_is_explicit(tmp_path, monkeypatch, error, status):
    instance = engine(tmp_path)
    monkeypatch.setattr(instance, "forward", AsyncMock(side_effect=error))
    with pytest.raises(web.HTTPException) as caught:
        asyncio.run(instance.handle(object(), "a", "http://node-0:26657", object()))
    assert caught.value.status == status


def test_history_proof_route_is_allowlisted_and_faulted(tmp_path):
    request = proxy.inspect_request("GET", "/evidence_history", {"height": "99"}, b"")
    assert request == proxy.RpcRead("evidence_history", 99)
    assert proxy.historical(request, 100)
    original = {
        "result": {
            "header": {"app_hash": "a" * 64},
            "validators": {"validators": []},
            "commit": {"height": "99"},
        }
    }
    changed = proxy.wrong_commit(original, method="evidence_history")
    assert changed["result"]["header"]["app_hash"] == "0" + "a" * 63
    assert changed["result"]["validators"] == original["result"]["validators"]
    assert changed["result"]["commit"] == original["result"]["commit"]
    assert original["result"]["header"]["app_hash"] == "a" * 64


@pytest.mark.parametrize("fail_second", [False, True])
def test_proxy_lifecycle_keeps_both_readers_and_cleans_up(tmp_path, monkeypatch, fail_second):
    handlers = []
    started = []
    cleaned = []
    forwarded = []

    class Router:
        def add_route(self, method, path, handler):
            handlers.append(handler)

    class App:
        def __init__(self, **kwargs):
            self.router = Router()
            self.index = len(handlers)

    class Runner:
        def __init__(self, app):
            self.app = app

        async def setup(self):
            pass

        async def cleanup(self):
            cleaned.append(self.app.index)

    class Site:
        def __init__(self, runner, host, port):
            self.runner = runner
            self.host = host
            self.port = port

        async def start(self):
            started.append((self.host, self.port))
            if fail_second and self.port == 26658:
                raise RuntimeError("second bind failed")

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class Stop:
        async def wait(self):
            raise asyncio.CancelledError

    async def observe(self, session, peer, origin, request):
        forwarded.append((peer, origin))
        return web.Response()

    monkeypatch.setattr(proxy.web, "Application", App)
    monkeypatch.setattr(proxy.web, "AppRunner", Runner)
    monkeypatch.setattr(proxy.web, "TCPSite", Site)
    monkeypatch.setattr(proxy.aiohttp, "ClientSession", lambda **kwargs: Session())
    monkeypatch.setattr(proxy.asyncio, "Event", Stop)
    monkeypatch.setattr(proxy.HistoryProxy, "handle", observe)
    args = SimpleNamespace(
        checkpoint=100,
        control=control(tmp_path),
        events=tmp_path / "events",
        hold_timeout=60,
        upstream_a="http://node-0:26657",
        upstream_b="http://node-1:26657",
        port_a=26657,
        port_b=26658,
        bind_host="127.0.0.1",
    )
    with pytest.raises(RuntimeError if fail_second else asyncio.CancelledError):
        asyncio.run(proxy.serve(args))
    assert started == [("127.0.0.1", 26657), ("127.0.0.1", 26658)]
    assert cleaned == [1, 0]
    for handler in handlers:
        asyncio.run(handler(object()))
    assert forwarded == [("a", "http://node-0:26657"), ("b", "http://node-1:26657")]


@pytest.mark.parametrize("checkpoint,timeout", [(1, 60), (100, 0)])
def test_proxy_refuses_invalid_start_parameters(checkpoint, timeout):
    with pytest.raises(ValueError):
        asyncio.run(proxy.serve(SimpleNamespace(checkpoint=checkpoint, hold_timeout=timeout)))
