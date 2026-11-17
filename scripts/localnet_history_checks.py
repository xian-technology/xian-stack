"""Committed evidence-policy assertions for the history-aware localnet gate."""

from __future__ import annotations

import asyncio
import base64
import json
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from localnet_chain_checks import rpc, wait_height
from localnet_rng_checks import timestamp_nanoseconds

MAX_INT64 = 2**63 - 1
EVIDENCE_FIELDS = frozenset({"max_age_num_blocks", "max_age_duration", "max_bytes"})
POLICY_FIELDS = frozenset({"schema_version", "height", "app_hash", "last_block_nanos", "evidence"})


@dataclass(frozen=True)
class PolicyView:
    height: int
    app_hash: str
    last_block_nanos: int
    evidence: Mapping[str, str]


def exact_fields(value: object, fields: frozenset[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"Invalid {label} fields")
    return value


def decimal_string(value: object, *, minimum: int | None, maximum: int | None) -> int:
    if not isinstance(value, str) or re.fullmatch(r"-?(?:0|[1-9][0-9]*)", value) is None:
        raise ValueError("Expected canonical decimal string")
    number = int(value)
    if str(number) != value:
        raise ValueError("Noncanonical decimal string")
    if minimum is not None and number < minimum:
        raise ValueError("Decimal value below minimum")
    if maximum is not None and number > maximum:
        raise ValueError("Decimal value above maximum")
    return number


def parse_policy(value: object) -> PolicyView:
    body = exact_fields(value, POLICY_FIELDS, "evidence policy")
    if type(body["schema_version"]) is not int or body["schema_version"] != 1:
        raise ValueError("Evidence policy schema_version must be integer 1")
    if (
        not isinstance(body["app_hash"], str)
        or re.fullmatch(r"[0-9a-f]{64}", body["app_hash"]) is None
    ):
        raise ValueError("Evidence policy app hash must be lowercase hexadecimal")
    evidence = exact_fields(body["evidence"], EVIDENCE_FIELDS, "evidence")
    for name, raw in evidence.items():
        decimal_string(raw, minimum=0 if name == "max_bytes" else 1, maximum=MAX_INT64)
    return PolicyView(
        height=decimal_string(body["height"], minimum=1, maximum=MAX_INT64),
        app_hash=body["app_hash"],
        last_block_nanos=decimal_string(body["last_block_nanos"], minimum=None, maximum=None),
        evidence=MappingProxyType(dict(evidence)),
    )


def validate_evidence_update(results: dict, expected: Mapping[str, str]) -> None:
    updates = results.get("consensus_param_updates")
    if updates is None:
        updates = {}
    if not isinstance(updates, dict):
        raise ValueError("Malformed consensus parameter update")
    if set(updates) != {"evidence"} or updates["evidence"] != expected:
        raise ValueError("Evidence updates must emit the complete evidence triple only")


def validate_policy_link(
    view: PolicyView,
    header: dict,
    next_header: dict,
    previous: dict,
    current: dict,
    results: dict,
) -> None:
    checks = (
        (header.get("height"), str(view.height), "committed height"),
        (next_header.get("height"), str(view.height + 1), "next height"),
        (str(next_header.get("app_hash")).lower(), view.app_hash, "committed app hash"),
        (timestamp_nanoseconds(header["time"]), view.last_block_nanos, "committed block time"),
        (current, view.evidence, "next-height evidence parameters"),
    )
    for actual, expected, label in checks:
        if actual != expected:
            raise ValueError(f"Mismatched {label}")
    if previous["max_bytes"] != current["max_bytes"]:
        raise ValueError("Unchanged configured evidence byte limit drifted")
    old_blocks = decimal_string(previous["max_age_num_blocks"], minimum=1, maximum=MAX_INT64)
    new_blocks = int(view.evidence["max_age_num_blocks"])
    if view.height - new_blocks < view.height - 1 - old_blocks:
        raise ValueError("Evidence block cutoff moved backward")
    before = decimal_string(previous["max_age_duration"], minimum=1, maximum=MAX_INT64)
    after = int(view.evidence["max_age_duration"])
    if after < before:
        raise ValueError("Evidence retention shrank")
    validate_evidence_update(results, view.evidence)


def policy_response_body(response: dict) -> dict:
    if response.get("code", 0) != 0 or response.get("info") != "dict":
        raise ValueError("Committed evidence query failed")
    if base64.b64decode(response.get("key", ""), validate=True) != b"evidence_policy":
        raise ValueError("Wrong evidence policy query key")
    raw = response.get("value")
    if not isinstance(raw, str) or len(raw) > 10_924:
        raise ValueError("Evidence policy response exceeds limit")
    data = base64.b64decode(raw, validate=True)
    if len(data) > 8192:
        raise ValueError("Evidence policy response exceeds limit")
    return json.loads(data)


async def committed_policy(session, node) -> tuple[PolicyView, dict]:
    response = (await rpc(session, node, "abci_query", path=json.dumps("/evidence_policy")))[
        "response"
    ]
    view = parse_policy(policy_response_body(response))
    if decimal_string(response.get("height"), minimum=1, maximum=MAX_INT64) != view.height:
        raise ValueError("Evidence query envelope height differs from committed body")
    return view, response


async def collect_policy_proof(session, node) -> dict:
    view, response = await committed_policy(session, node)
    await wait_height(session, node, view.height + 1)
    header, next_header, previous, current, results = await asyncio.gather(
        rpc(session, node, "block", height=view.height),
        rpc(session, node, "block", height=view.height + 1),
        rpc(session, node, "consensus_params", height=view.height),
        rpc(session, node, "consensus_params", height=view.height + 1),
        rpc(session, node, "block_results", height=view.height),
    )
    validate_policy_link(
        view,
        header["block"]["header"],
        next_header["block"]["header"],
        previous["consensus_params"]["evidence"],
        current["consensus_params"]["evidence"],
        results,
    )
    return {
        "response": response,
        "header": header,
        "next_header": next_header,
        "previous_params": previous,
        "next_params": current,
        "block_results": results,
    }
