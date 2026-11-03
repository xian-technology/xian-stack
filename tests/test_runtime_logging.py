from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import shielded_relayer_service as relayer


def test_relayer_formatter_is_utc_single_line_and_context_is_structured():
    from runtime_logging import ServiceFormatter

    record = logging.LogRecord("worker", logging.WARNING, "", 0, "bad\nmessage", (), None)
    record.created = 0
    record.context = {"route": "/v1/{id}", "reason": "bad\rinput"}
    line = ServiceFormatter("shielded-relayer").format(record)
    assert line.startswith("1970-01-01T00:00:00.000Z | WARNING | shielded-relayer/worker | ")
    assert "\n" not in line and "\r" not in line
    assert "route=/v1/{id}" in line
    assert "reason=bad\\rinput" in line


def test_failed_request_logs_safe_reason_and_generated_id_without_payload():
    secret = "private-key-secret-and-request-payload"
    request = SimpleNamespace(
        match_info=SimpleNamespace(route=SimpleNamespace(resource=None)),
        path=f"/{secret}",
        method="POST",
        app={},
        headers={"X-Request-ID": secret},
    )

    async def fail(_request):
        raise RuntimeError(secret)

    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = Capture()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        response = asyncio.run(relayer._error_middleware(request, fail))
    finally:
        root.removeHandler(handler)
    assert response.status == 500
    assert records and records[-1].levelno == logging.ERROR
    assert records[-1].exc_info is None
    context = records[-1].context
    assert context["route"] == "<unmatched>"
    assert context["error_type"] == "RuntimeError"
    assert context["reason"] == "Internal Server Error"
    assert context["request_id"] == response.headers["X-Request-ID"]
    assert secret not in str(context)
    assert secret not in records[-1].getMessage()


def test_formatter_bounds_context_and_never_renders_arbitrary_objects():
    from runtime_logging import ServiceFormatter

    class Dangerous:
        def __str__(self):
            raise AssertionError("must not call arbitrary str")

        def __repr__(self):
            raise AssertionError("must not call arbitrary repr")

    record = logging.LogRecord("worker", logging.ERROR, "", 0, "%999999999s", (Dangerous(),), None)
    record.context = {"reason": "x" * 2_000_000, "password": "secret", "request_id": Dangerous()}
    line = ServiceFormatter("test").format(record)
    assert len(line) < 1500
    assert "secret" not in line and "password" not in line
    assert "request_id=<unsupported>" in line
