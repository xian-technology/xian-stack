import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from localnet_json_logging import execution_hash, trace_record
from test_localnet_block_ordering_phase import localnet_e2e as e2e

TX = "a" * 64


@pytest.mark.parametrize(
    "execution", [None, {}, {"hash": None}, {"hash": "a" * 63}, {"hash": "G" * 64}]
)
def test_missing_or_invalid_execution_hash_fails_clearly(execution):
    with pytest.raises(e2e.E2EError, match="receipt execution hash is missing or invalid"):
        execution_hash(execution)


def serialized_record():
    # Loguru serialize=True envelope, including rendered text and structured metadata.
    extra = {
        "stage": "finalize_tx_result",
        "tx_hash": TX,
        "block_height": 17,
        "block_hash": "b" * 64,
        "tx_index": 0,
        "sender": "c" * 64,
        "contract": "currency",
        "function": "transfer",
        "nonce": 0,
        "status": 0,
        "service": "xian-abci",
        "pid": 42,
        "receipt_bytes": 729,
        "context": f" | stage=finalize_tx_result tx_hash={TX} receipt_bytes=729",
    }
    return {
        "text": (
            "2026-11-03T12:34:56.123Z | TRACE | xian-abci/finalize | "
            "Encoded finalized transaction receipt"
        ),
        "record": {
            "level": {"name": "TRACE", "no": 5},
            "time": {"repr": "2026-11-03T12:34:56.123456+00:00", "timestamp": 1793709296.123456},
            "process": {"id": 42, "name": "MainProcess"},
            "message": "Encoded finalized transaction receipt",
            "extra": extra,
        },
    }


def test_extracts_structured_trace_for_submitted_hash_only():
    row = trace_record(json.dumps(serialized_record()), {TX})
    assert row["tx_hash"] == TX and row["receipt_bytes"] == 729
    assert row["timestamp"] == "2026-11-03T12:34:56.123456+00:00"
    assert trace_record(json.dumps(serialized_record()), {"d" * 64}) is None


@pytest.mark.parametrize(
    "line", ["not json", "{broken", "{}", "[]", '{"record":null}', '"stage=finalize_tx_result"']
)
def test_malformed_or_missing_json_record_does_not_match(line):
    assert trace_record(line, {TX}) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("receipt_bytes", None),
        ("receipt_bytes", True),
        ("receipt_bytes", 0),
        ("block_height", "17"),
        ("tx_index", -1),
        ("service", "wrong"),
        ("pid", 0),
        ("context", "unrelated"),
        ("result", {"secret": "payload-canary"}),
    ],
)
def test_invalid_correlated_trace_fails_closed(field, value):
    record = serialized_record()
    record["record"]["extra"][field] = value
    if field == "receipt_bytes":
        record["record"]["extra"]["context"] = (
            f" | stage=finalize_tx_result tx_hash={TX} receipt_bytes={value}"
        )
    with pytest.raises(e2e.E2EError, match="JSON TRACE") as failure:
        trace_record(json.dumps(record), {TX})
    assert "payload-canary" not in str(failure.value)


@pytest.mark.parametrize("mutation", ["offset", "invalid_date", "plain_time", "raw_message"])
def test_trace_requires_utc_and_metadata_message(mutation):
    record = serialized_record()
    if mutation == "offset":
        record["record"]["time"]["repr"] = "2026-11-03T13:34:56.123456+01:00"
    elif mutation == "invalid_date":
        record["record"]["time"]["repr"] = "2026-99-03T12:34:56+00:00"
    elif mutation == "plain_time":
        record["text"] = "local time without UTC marker"
    else:
        record["record"]["message"] = '{"result":"payload-canary"}'
    with pytest.raises(e2e.E2EError, match="JSON TRACE"):
        trace_record(json.dumps(record), {TX})


def test_phase_runs_debug_plain_trace_json_then_info_plain(monkeypatch):
    runner = object.__new__(e2e.E2ERunner)
    runner.nodes = [SimpleNamespace(moniker=f"node-{i}") for i in range(5)]
    runner.seed = "public-test-seed"
    runner.founder_wallet = SimpleNamespace(public_key="founder")
    runner.args = SimpleNamespace(log_level="INFO", rpc_timeout_seconds=30)
    runner.restart_localnet_and_wait_ready = AsyncMock()
    runner.fund_wallets = AsyncMock()
    runner.stabilize_nodes = AsyncMock()
    modes = []
    monkeypatch.setattr(e2e, "update_logging_config", lambda **kw: modes.append(kw))
    monkeypatch.setattr(e2e, "log_positions_by_node", lambda nodes: {})
    monkeypatch.setattr(e2e, "derive_wallet", lambda seed, label: SimpleNamespace(public_key=label))
    monkeypatch.setattr(e2e, "collect_log_matches", lambda *a, **kw: {"node-0": ["stage=check_tx"]})

    @asynccontextmanager
    async def client(wallet, *args):
        if wallet.public_key == "logging-checktx-reject":
            submission = SimpleNamespace(accepted=False)
        else:
            submission = SimpleNamespace(
                submitted=True,
                accepted=True,
                finalized=True,
                tx_hash="D" * 64,
                nonce=0,
                chi_supplied=500,
                message=None,
                receipt=SimpleNamespace(
                    success=True,
                    message=None,
                    execution={"hash": TX, "chi_used": 253},
                    raw={"result": {"height": "17", "index": 0}},
                ),
            )
        yield SimpleNamespace(send=AsyncMock(return_value=submission))

    runner.client = client

    async def matches(nodes, positions, matcher, **kwargs):
        line = (
            json.dumps(serialized_record())
            if modes[-1]["json_logging"]
            else "DEBUG stage=execute_tx"
        )
        assert matcher(line)
        if modes[-1]["json_logging"]:
            assert not matcher(
                json.dumps(
                    {
                        **serialized_record(),
                        "record": {
                            **serialized_record()["record"],
                            "extra": {
                                **serialized_record()["record"]["extra"],
                                "tx_hash": "e" * 64,
                            },
                        },
                    }
                )
            )
        return {node.moniker: [line] for node in nodes}

    monkeypatch.setattr(e2e, "wait_for_log_matches", matches)
    result = asyncio.run(runner.logging_phase(None))
    assert [(m["level"], m["json_logging"]) for m in modes] == [
        ("DEBUG", False),
        ("TRACE", True),
        ("INFO", False),
    ]
    assert runner.restart_localnet_and_wait_ready.await_count == 3
    assert len(result["trace_records"]) == 5
    assert result["trace_receipts"][0]["tx_hash"] == "D" * 64
    assert result["trace_receipts"][0]["execution_hash"] == TX


@pytest.mark.parametrize("restore_fails", [False, True])
def test_failed_phase_restores_config_preserves_primary_error_without_restart(
    monkeypatch, restore_fails
):
    runner = object.__new__(e2e.E2ERunner)
    runner._logging_phase_checks = AsyncMock(side_effect=RuntimeError("primary failure"))
    runner.restart_localnet_and_wait_ready = AsyncMock()

    def restore(**kwargs):
        assert kwargs == {"level": "INFO", "trace_logging": False, "json_logging": False}
        if restore_fails:
            raise ValueError("cleanup failure")

    monkeypatch.setattr(e2e, "update_logging_config", restore)
    with pytest.raises(RuntimeError, match="primary failure"):
        asyncio.run(runner.logging_phase(None))
    runner.restart_localnet_and_wait_ready.assert_not_awaited()


def test_accepts_actual_node_serialized_loguru_record():
    from loguru import logger
    from xian.app_logging import _PLAIN_LOG_FORMAT, build_log_fields
    from xian.log_safety import sanitize_record

    captured = []
    sink = logger.add(captured.append, level="TRACE", serialize=True, format=_PLAIN_LOG_FORMAT)
    try:
        logger.patch(sanitize_record).bind(
            **build_log_fields(
                stage="finalize_tx_result",
                tx_hash=TX,
                block_height=17,
                block_hash="b" * 64,
                tx_index=0,
                status=0,
                payload={
                    "sender": "c" * 64,
                    "contract": "currency",
                    "function": "transfer",
                    "nonce": 0,
                },
                extra={"receipt_bytes": 729},
            )
        ).trace("Encoded finalized transaction receipt")
    finally:
        logger.remove(sink)
    result = trace_record(str(captured[-1]), {TX})
    assert result["tx_hash"] == TX and result["receipt_bytes"] == 729


def test_missing_process_context_is_a_controlled_failure():
    record = serialized_record()
    record["record"]["process"] = None
    with pytest.raises(e2e.E2EError, match="JSON TRACE"):
        trace_record(json.dumps(record), {TX})
