from __future__ import annotations

import asyncio
import base64
import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).absolute().parents[1] / "scripts"))
import localnet_history_checks as history


def body():
    return {
        "schema_version": 1,
        "height": "10",
        "app_hash": "a" * 64,
        "last_block_nanos": "10000000000",
        "evidence": {
            "max_age_num_blocks": "100000",
            "max_age_duration": "100",
            "max_bytes": "1048576",
        },
    }


def test_committed_policy_preserves_exact_string_integers():
    data = body()
    data["last_block_nanos"] = "253402300799123456789"
    view = history.parse_policy(data)
    assert view.height == 10 and view.app_hash == "a" * 64
    assert view.last_block_nanos == 253402300799123456789
    assert view.evidence == {
        "max_age_num_blocks": "100000",
        "max_age_duration": "100",
        "max_bytes": "1048576",
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("height", "01"),
        ("height", 1),
        ("height", "0"),
        ("height", True),
        ("app_hash", "A" * 64),
        ("schema_version", True),
        ("schema_version", 2),
        ("last_block_nanos", "-0"),
        ("last_block_nanos", "+1"),
    ],
)
def test_committed_policy_rejects_ambiguous_wire_values(field, value):
    data = body()
    data[field] = value
    with pytest.raises(ValueError):
        history.parse_policy(data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_age_num_blocks", "0"),
        ("max_age_duration", "-1"),
        ("max_bytes", "-1"),
        ("max_age_duration", 1),
        ("max_age_duration", str(2**63)),
    ],
)
def test_evidence_fields_are_canonical_comet_int64(field, value):
    data = body()
    data["evidence"][field] = value
    with pytest.raises(ValueError):
        history.parse_policy(data)


def test_policy_schema_is_exact_and_max_bytes_may_be_zero():
    data = body()
    data["evidence"]["max_bytes"] = "0"
    history.parse_policy(data)
    for candidate in [None, [], dict(data, unknown=True), dict(data, evidence={})]:
        with pytest.raises(ValueError):
            history.parse_policy(candidate)


def link_values():
    data = body()
    previous = {"max_age_num_blocks": "100000", "max_age_duration": "99", "max_bytes": "1048576"}
    header = {"height": "10", "time": "1970-01-01T00:00:10Z"}
    next_header = {"height": "11", "app_hash": "A" * 64}
    updates = {"consensus_param_updates": {"evidence": copy.deepcopy(data["evidence"])}}
    return data, header, next_header, previous, copy.deepcopy(data["evidence"]), updates


def test_policy_and_nextheight_params_are_linked_to_exact_committed_header():
    values = link_values()
    history.validate_policy_link(history.parse_policy(values[0]), *values[1:])


@pytest.mark.parametrize(
    "change",
    [
        "wrong_height",
        "wrong_next",
        "wrong_hash",
        "wrong_time",
        "wrong_params",
        "reopened_blocks",
        "changed_bytes",
        "missing_growth",
        "partial_growth",
        "unrelated_update",
        "shrinking",
    ],
)
def test_policy_link_rejects_divergent_commit_or_param_updates(change):
    values = list(link_values())
    if change == "wrong_height":
        values[1]["height"] = "9"
    elif change == "wrong_next":
        values[2]["height"] = "12"
    elif change == "wrong_hash":
        values[2]["app_hash"] = "b" * 64
    elif change == "wrong_time":
        values[1]["time"] = "1970-01-01T00:00:11Z"
    elif change == "wrong_params":
        values[4]["max_age_duration"] = "101"
    elif change == "reopened_blocks":
        values[3]["max_age_num_blocks"] = "99998"
    elif change == "changed_bytes":
        values[3]["max_bytes"] = "0"
    elif change == "missing_growth":
        values[5] = {}
    elif change == "partial_growth":
        values[5]["consensus_param_updates"]["evidence"].pop("max_bytes")
    elif change == "unrelated_update":
        values[5]["consensus_param_updates"]["block"] = {"max_bytes": "10"}
    elif change == "shrinking":
        values[3]["max_age_duration"] = "101"
    with pytest.raises(ValueError):
        history.validate_policy_link(history.parse_policy(values[0]), *values[1:])


def test_unchanged_duration_accepts_only_an_exact_idempotent_evidence_update():
    data, header, next_header, previous, current, _ = link_values()
    previous["max_age_duration"] = "100"
    with pytest.raises(ValueError, match="complete evidence triple"):
        history.validate_policy_link(
            history.parse_policy(data), header, next_header, previous, current, {}
        )
    history.validate_policy_link(
        history.parse_policy(data),
        header,
        next_header,
        previous,
        current,
        {"consensus_param_updates": {"evidence": current}},
    )
    for updates in (
        {"evidence": {"max_age_duration": current["max_age_duration"]}},
        {"evidence": {**current, "max_bytes": "0"}},
        {"evidence": current, "block": {"max_bytes": "10"}},
    ):
        with pytest.raises(ValueError):
            history.validate_evidence_update({"consensus_param_updates": updates}, current)


def test_committed_query_height_and_body_agree(monkeypatch):
    data = body()
    response = {
        "code": 0,
        "info": "dict",
        "key": base64.b64encode(b"evidence_policy").decode(),
        "height": "10",
        "value": base64.b64encode(json.dumps(data).encode()).decode(),
    }
    call = AsyncMock(return_value={"response": response})
    monkeypatch.setattr(history, "rpc", call)
    view, raw = asyncio.run(history.committed_policy(object(), SimpleNamespace(moniker="node0")))
    assert view.height == 10 and raw == response
    assert call.call_args.kwargs["path"] == json.dumps("/evidence_policy")
    response["height"] = "9"
    with pytest.raises(ValueError):
        asyncio.run(history.committed_policy(object(), SimpleNamespace(moniker="node0")))


@pytest.mark.parametrize(
    "changes",
    [
        {"code": 1},
        {"info": "str"},
        {"key": base64.b64encode(b"wrong").decode()},
        {"value": "*"},
        {"value": "a" * 10925},
        {"value": base64.b64encode(b" " * 8193).decode()},
    ],
)
def test_policy_query_errors_and_payload_limits_are_explicit(changes):
    response = {
        "code": 0,
        "info": "dict",
        "key": base64.b64encode(b"evidence_policy").decode(),
        "value": base64.b64encode(json.dumps(body()).encode()).decode(),
    }
    response.update(changes)
    with pytest.raises(ValueError):
        history.policy_response_body(response)


def test_policy_proof_waits_then_uses_archived_exact_heights(monkeypatch):
    data, header, next_header, previous, current, updates = link_values()
    seen = []
    ready = []
    monkeypatch.setattr(
        history,
        "committed_policy",
        AsyncMock(return_value=(history.parse_policy(data), {"height": "10"})),
    )

    async def wait(session, node, target):
        assert target == 11
        ready.append(True)

    async def archived(session, node, method, **params):
        assert ready, "Pinned next-height read must wait for commitment"
        seen.append((method, params["height"]))
        if method == "block":
            return {"block": {"header": header if params["height"] == 10 else next_header}}
        if method == "consensus_params":
            return {
                "consensus_params": {"evidence": previous if params["height"] == 10 else current}
            }
        return updates

    monkeypatch.setattr(history, "wait_height", wait)
    monkeypatch.setattr(history, "rpc", archived)
    result = asyncio.run(history.collect_policy_proof(object(), object()))
    assert sorted(seen) == sorted(
        [
            ("block", 10),
            ("block", 11),
            ("consensus_params", 10),
            ("consensus_params", 11),
            ("block_results", 10),
        ]
    )
    assert (
        result["response"] == {"height": "10"}
        and result["next_params"]["consensus_params"]["evidence"] == current
    )


def test_parsed_policy_snapshot_cannot_be_changed_by_source_writer():
    data = body()
    view = history.parse_policy(data)
    data["evidence"]["max_bytes"] = "0"
    assert view.evidence["max_bytes"] == "1048576"
    with pytest.raises(TypeError):
        view.evidence["max_bytes"] = "0"


def test_malformed_parameter_update_is_never_treated_as_no_change():
    with pytest.raises(ValueError):
        history.validate_evidence_update({"consensus_param_updates": []}, {})


def test_derived_block_retention_may_grow_one_per_height_or_drop_at_safe_rotation():
    for previous_blocks, current_blocks in (("100000", "100001"), ("100001", "100000")):
        data, header, next_header, previous, current, _ = link_values()
        previous["max_age_num_blocks"] = previous_blocks
        previous["max_age_duration"] = current["max_age_duration"]
        current["max_age_num_blocks"] = current_blocks
        data["evidence"]["max_age_num_blocks"] = current_blocks
        update = {"consensus_param_updates": {"evidence": dict(current)}}
        history.validate_policy_link(
            history.parse_policy(data), header, next_header, previous, current, update
        )
        with pytest.raises(ValueError, match="complete evidence"):
            history.validate_policy_link(
                history.parse_policy(data), header, next_header, previous, current, {}
            )


def test_committed_wire_accepts_the_exact_comet_int64_boundary():
    data = body()
    data["evidence"] = {name: str(2**63 - 1) for name in data["evidence"]}
    assert dict(history.parse_policy(data).evidence) == data["evidence"]


def test_query_body_default_zero_code_and_exact_payload_budget_are_valid():
    encoded = json.dumps(body()).encode()
    padded = encoded + b" " * (8192 - len(encoded))
    response = {
        "info": "dict",
        "key": base64.b64encode(b"evidence_policy").decode(),
        "value": base64.b64encode(padded).decode(),
    }
    assert len(response["value"]) == 10924
    assert history.policy_response_body(response) == body()


@pytest.mark.parametrize("field", ["key", "value"])
def test_query_body_refuses_noncanonical_base64_with_ignored_characters(field):
    response = {
        "code": 0,
        "info": "dict",
        "key": base64.b64encode(b"evidence_policy").decode(),
        "value": base64.b64encode(json.dumps(body()).encode()).decode(),
    }
    response[field] = "!" + response[field]
    with pytest.raises(ValueError):
        history.policy_response_body(response)


def test_query_body_refuses_bytes_even_when_they_encode_valid_json():
    response = {
        "code": 0,
        "info": "dict",
        "key": base64.b64encode(b"evidence_policy").decode(),
        "value": base64.b64encode(json.dumps(body()).encode()),
    }
    with pytest.raises(ValueError):
        history.policy_response_body(response)


def test_policy_link_refuses_an_invalid_zero_previous_duration():
    data, header, next_header, previous, current, updates = link_values()
    previous["max_age_duration"] = "0"
    with pytest.raises(ValueError):
        history.validate_policy_link(
            history.parse_policy(data), header, next_header, previous, current, updates
        )
