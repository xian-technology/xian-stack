"""Controlled read-only RPC faults for an isolated history-bootstrap rehearsal.

Run only on the review Docker bridge, without publishing its listening ports.
The operator-owned control file switches pass/hold/wrong_commit behavior; the
proxy never exposes a remote control route and never forwards transaction calls.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

MAX_REQUEST_BYTES = 65_536
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
READ_METHODS = frozenset(
    {
        "status",
        "health",
        "net_info",
        "commit",
        "validators",
        "consensus_params",
        "abci_query",
        "abci_info",
        "block",
        "header",
        "blockchain",
        "genesis",
        "genesis_chunked",
        "evidence_history",
    }
)
HISTORY_METHODS = frozenset({"commit", "validators", "consensus_params", "evidence_history"})
LOCAL_UPSTREAMS = frozenset({"node-0", "node-1", "localhost", "127.0.0.1", "::1"})


@dataclass(frozen=True)
class RpcRead:
    method: str
    height: int | None


@dataclass(frozen=True)
class Settings:
    mode: str
    targets: frozenset[str]


def settings_from_json(value: object) -> Settings:
    if not isinstance(value, dict) or set(value) != {"mode", "targets"}:
        raise ValueError("Control must contain exactly mode and targets")
    mode, targets = value["mode"], value["targets"]
    if mode not in ("pass", "hold", "wrong_commit"):
        raise ValueError("Unsupported history proxy mode")
    if (
        not isinstance(targets, list)
        or not targets
        or any(item not in ("a", "b") for item in targets)
    ):
        raise ValueError("Control targets must be a nonempty list of a/b")
    return Settings(mode, frozenset(targets))


def load_settings(path: Path) -> Settings:
    with path.open("rb") as handle:
        data = handle.read(4097)
    if len(data) > 4096:
        raise ValueError("History proxy control exceeds 4096 bytes")
    return settings_from_json(json.loads(data))


def rpc_height(value: object) -> int | None:
    if value is None or value == "0" or type(value) is int and value == 0:
        return None
    if type(value) is int and value > 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value):
        return int(value)
    raise ValueError("RPC height must be a canonical positive integer or latest")


def inspect_request(method: str, path: str, query: dict, body: bytes) -> RpcRead:
    if len(body) > MAX_REQUEST_BYTES:
        raise ValueError("History proxy request too large")
    if method == "GET":
        rpc_method, params = path.strip("/"), query
    elif method == "POST":
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("History proxy requires a single JSON-RPC request")
        rpc_method, params = payload.get("method"), payload.get("params", {})
    else:
        raise ValueError("History proxy supports only GET/POST")
    if (
        not isinstance(rpc_method, str)
        or rpc_method not in READ_METHODS
        or not isinstance(params, dict)
    ):
        raise ValueError("History proxy accepts read-only RPC methods")
    return RpcRead(rpc_method, rpc_height(params.get("height")))


def historical(request: RpcRead, checkpoint: int) -> bool:
    return (
        request.method in HISTORY_METHODS
        and request.height is not None
        and request.height < checkpoint
    )


def upstream_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in LOCAL_UPSTREAMS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Upstream must be a local review node HTTP origin")
    return value.rstrip("/")


def wrong_commit(value: dict, *, method: str = "commit") -> dict:
    result = copy.deepcopy(value)
    if method == "evidence_history":
        header = result["result"]["header"]
    else:
        header = result["result"]["signed_header"]["header"]
    app_hash = header["app_hash"]
    if not isinstance(app_hash, str) or re.fullmatch(r"[0-9a-fA-F]{64}", app_hash) is None:
        raise ValueError("Expected a canonical committed header app hash")
    header["app_hash"] = ("1" if app_hash[0] == "0" else "0") + app_hash[1:]
    return result


async def bounded_body(stream) -> bytes:
    body = bytearray()
    async for chunk in stream.iter_chunked(65_536):
        body.extend(chunk)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("History proxy response exceeds limit")
    return bytes(body)


class HistoryProxy:
    def __init__(self, *, checkpoint: int, control: Path, events: Path, hold_timeout: float):
        self.checkpoint = checkpoint
        self.control = control
        self.events = events
        self.hold_timeout = hold_timeout
        self.semaphore = asyncio.Semaphore(4)
        load_settings(control)

    def record(self, kind: str, peer: str, request: RpcRead) -> None:
        event = {
            "schema_version": 1,
            "kind": kind,
            "peer": peer,
            "method": request.method,
            "height": request.height,
            "at_ns": time.time_ns(),
        }
        with self.events.open("a") as handle:
            handle.write(json.dumps(event, separators=(",", ":")) + "\n")

    async def wait_for_release(self, peer: str, request: RpcRead) -> Settings:
        settings = load_settings(self.control)
        if not historical(request, self.checkpoint) or peer not in settings.targets:
            return Settings("pass", settings.targets)
        if settings.mode != "hold":
            return settings
        self.record("history-held", peer, request)
        deadline = time.monotonic() + self.hold_timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            settings = load_settings(self.control)
            if peer not in settings.targets:
                return Settings("pass", settings.targets)
            if settings.mode != "hold":
                return settings
        raise TimeoutError("Controlled history hold expired")

    async def forward(self, session, peer: str, origin: str, request) -> web.Response:
        body = await request.read()
        read = inspect_request(request.method, request.path, dict(request.query), body)
        settings = await self.wait_for_release(peer, read)
        async with session.request(
            request.method,
            origin + str(request.rel_url),
            data=body,
            headers={"Content-Type": request.content_type},
        ) as response:
            payload = await bounded_body(response.content)
            if settings.mode == "wrong_commit" and read.method in ("commit", "evidence_history"):
                payload = json.dumps(wrong_commit(json.loads(payload), method=read.method)).encode()
                self.record("wrong-commit-sent", peer, read)
            else:
                self.record("forwarded", peer, read)
            return web.Response(
                status=response.status,
                body=payload,
                headers={"Content-Type": response.headers.get("Content-Type", "application/json")},
            )

    async def handle(self, session, peer: str, origin: str, request) -> web.Response:
        async with self.semaphore:
            try:
                return await self.forward(session, peer, origin, request)
            except (ValueError, KeyError, json.JSONDecodeError) as error:
                raise web.HTTPBadRequest(text=str(error)) from error
            except TimeoutError as error:
                raise web.HTTPGatewayTimeout(text=str(error)) from error


async def serve(args):
    if args.checkpoint <= 1 or args.hold_timeout <= 0:
        raise ValueError("Positive checkpoint and hold timeout required")
    proxy = HistoryProxy(
        checkpoint=args.checkpoint,
        control=args.control,
        events=args.events,
        hold_timeout=args.hold_timeout,
    )
    runners = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        try:
            for peer, origin, port in (
                ("a", upstream_url(args.upstream_a), args.port_a),
                ("b", upstream_url(args.upstream_b), args.port_b),
            ):
                app = web.Application(client_max_size=MAX_REQUEST_BYTES)

                async def handle(request, peer=peer, origin=origin):
                    return await proxy.handle(session, peer, origin, request)

                app.router.add_route("*", "/{tail:.*}", handle)
                runner = web.AppRunner(app)
                await runner.setup()
                runners.append(runner)
                await web.TCPSite(runner, args.bind_host, port).start()
            await asyncio.Event().wait()
        finally:
            for runner in reversed(runners):
                await runner.cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=int, required=True)
    parser.add_argument("--upstream-a", required=True)
    parser.add_argument("--upstream-b", required=True)
    parser.add_argument("--control", type=Path, required=True)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--bind-host", default="127.0.0.1")
    parser.add_argument("--port-a", type=int, default=26657)
    parser.add_argument("--port-b", type=int, default=26658)
    parser.add_argument("--hold-timeout", type=float, default=60)
    asyncio.run(serve(parser.parse_args()))


if __name__ == "__main__":
    main()
