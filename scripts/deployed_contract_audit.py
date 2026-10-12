#!/usr/bin/env python3
"""Read-only, height-consistent contract inventory. Never authorizes enrollment.

RPC responses are trusted evidence, not cryptographic state proofs. All supplied
validators must return the same complete inventory at one committed height.
"""

from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import json
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
MAX_ITEMS = 100_000
MAX_RESPONSE = 32 * 1024 * 1024


class AuditError(ValueError):
    pass


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def text_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Rpc:
    def __init__(self, url: str):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise AuditError("RPC must be an explicit HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise AuditError("Do not put credentials, query parameters or fragments in RPC URLs")
        self.url = url.rstrip("/")

    def get(self, route: str, **params: Any) -> dict:
        url = self.url + "/" + route + "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=15) as response:
            raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise AuditError("RPC response exceeds audit bound")
        body = json.loads(raw)
        if "error" in body or not isinstance(body.get("result"), dict):
            raise AuditError(f"RPC failed for {route}")
        return body["result"]

    def query(self, path: str, height: int = 0) -> dict:
        response = self.get("abci_query", path=json.dumps(path), height=height, prove="false")[
            "response"
        ]
        if response.get("code") != 0:
            raise AuditError(f"Query failed: {path}: {response.get('log', '')}")
        actual = int(response.get("height", 0))
        if actual <= 0 or (height and actual != height):
            raise AuditError("Query did not return the requested committed height")
        if "value" not in response:
            raise AuditError("Missing RPC value field")
        # Retain the exact wire type and value: decimal/string and integer must
        # not collapse into each other during preservation comparisons.
        return {"height": actual, "info": response.get("info", ""), "value": response["value"]}


def decoded(response: dict) -> Any:
    value = response["value"]
    if value is None:
        return "" if response.get("info") == "str" else None
    raw = base64.b64decode(value, validate=True)
    if raw == b"\0":
        return None
    text = raw.decode("utf-8")
    return json.loads(text) if response["info"] in {"dict", "list"} else text


def contract_names(rpc: Rpc, height: int = 0) -> tuple[int, list[str]]:
    names: list[str] = []
    total = None
    while True:
        result = rpc.query(f"contracts/limit=100/offset={len(names)}/sort=name/order=asc", height)
        height = result["height"]
        page = decoded(result)
        if not isinstance(page, dict) or type(page.get("total")) is not int:
            raise AuditError("Invalid contract listing")
        if total is None:
            total = page["total"]
        if total <= 0 or total > MAX_ITEMS or page["total"] != total:
            raise AuditError("Empty, excessive or changing contract inventory")
        if page.get("offset") != len(names) or not isinstance(page.get("items"), list):
            raise AuditError("Invalid contract pagination")
        batch = [entry["name"] for entry in page["items"]]
        if not batch or any(not isinstance(n, str) or not NAME.fullmatch(n) for n in batch):
            raise AuditError("Empty page or invalid contract name")
        combined = names + batch
        if combined != sorted(set(combined)) or len(combined) > total:
            raise AuditError("Duplicate, unordered or excessive contracts")
        names = combined
        if len(names) == total:
            return height, names


def storage_keys(rpc: Rpc, prefix: str, height: int) -> list[str]:
    keys: list[str] = []
    after = None
    while True:
        path = f"keys/{prefix}/limit=200"
        if after is not None:
            path += f"/after={after}"
        page = decoded(rpc.query(path, height))
        if not isinstance(page, dict) or page.get("prefix") != prefix:
            raise AuditError("Invalid storage listing")
        batch = page.get("items")
        if not isinstance(batch, list) or any(
            not isinstance(key, str) or not key or "/" in key for key in batch
        ):
            raise AuditError("Invalid or unsupported storage key")
        combined = keys + batch
        if combined != sorted(set(combined)) or len(combined) > MAX_ITEMS:
            raise AuditError("Duplicate, unordered or excessive storage keys")
        keys = combined
        if page.get("has_more") is False:
            if page.get("next_after") is not None:
                raise AuditError("Inconsistent final storage cursor")
            return [prefix + key for key in keys]
        if page.get("has_more") is not True or not batch or page.get("next_after") != batch[-1]:
            raise AuditError("Invalid or non-progressing storage cursor")
        after = batch[-1]


def capture(rpc: Rpc, chain_id: str, height: int = 0) -> dict:
    before = rpc.get("status")
    if before["node_info"]["network"] != chain_id or before["sync_info"]["catching_up"]:
        raise AuditError("Wrong chain or node is catching up")
    height, names = contract_names(rpc, height)
    keys = []
    for prefix in [*(name + "." for name in names), "_accounts."]:
        keys.extend(storage_keys(rpc, prefix, height))
    if len(keys) > MAX_ITEMS:
        raise AuditError("Excessive total storage inventory")
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = pool.map(lambda key: rpc.query("get/" + key, height), keys)
        state = dict(zip(keys, values, strict=True))
    contracts = []
    for name in names:
        source = decoded(state.get(name + ".__source__", {"value": None}))
        ir = decoded(state.get(name + ".__xian_ir_v1__", {"value": None}))
        if not isinstance(source, str) or not source or not isinstance(ir, str) or not ir:
            raise AuditError(f"Missing source or executable IR for {name}")
        json.loads(ir)
        contracts.append(
            {"name": name, "source_sha256": text_hash(source), "ir_sha256": text_hash(ir)}
        )
    policy = rpc.query("get/_accounts.policy", height)
    indexed_policy = state.get("_accounts.policy")
    if indexed_policy is None and decoded(policy) is not None:
        raise AuditError("Policy missing from storage inventory")
    if indexed_policy is not None and indexed_policy != policy:
        raise AuditError("Policy changed during collection")
    _, final_names = contract_names(rpc, height)
    if final_names != names:
        raise AuditError("Contract inventory changed during collection")
    # The next header commits this height's app hash; the current header's
    # app_hash is for H-1. Do not mislabel it as a proof of the queried state.
    header = rpc.get("block", height=height)["block"]["header"]
    if header["chain_id"] != chain_id or int(header["height"]) != height:
        raise AuditError("Invalid block header identity")
    return {
        "chain_id": chain_id,
        "height": height,
        "header": header,
        "contracts": contracts,
        "state": state,
        "policy": policy,
    }


def collect(urls: list[str], chain_id: str) -> dict:
    urls = [url.rstrip("/") for url in urls]
    if len(set(urls)) != len(urls) or len(urls) < 2:
        raise AuditError("Supply at least two distinct validator RPC endpoints")
    first = capture(Rpc(urls[0]), chain_id)
    with ThreadPoolExecutor(max_workers=min(8, len(urls) - 1)) as pool:
        peers = list(pool.map(lambda url: capture(Rpc(url), chain_id, first["height"]), urls[1:]))
    fingerprint = digest(first)
    if any(digest(peer) != fingerprint for peer in peers):
        raise AuditError("Validators disagree on contract source, IR, state or block header")
    return {
        "schema": 1,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "rpc_endpoints": urls,
        "snapshot_sha256": fingerprint,
        "snapshot": first,
    }


def review_hints(source: str) -> dict:
    """Navigation hints only. Absence of a hit is never a security verdict."""
    tree = ast.parse(source)
    calls = sorted(
        {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    )
    identifiers = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    imports = [
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    return {
        "calls": calls,
        "imports": imports,
        "signature_review": bool(identifiers & {"crypto", "signature", "signatures"})
        or any(re.search(r"verify|recover|signature", call, re.I) for call in calls),
    }


def report(inventory: dict) -> dict:
    snap = inventory["snapshot"]
    contracts = []
    for contract in snap["contracts"]:
        name = contract["name"]
        source = decoded(snap["state"][name + ".__source__"])
        contracts.append(
            {**contract, "review_status": "manual_review_required", **review_hints(source)}
        )
    policy = decoded(snap["policy"])
    absent = "_accounts.policy" not in snap["state"] and policy is None
    closed = isinstance(policy, dict) and policy.get("enrollment_open") is False
    return {
        "schema": 1,
        "chain_id": snap["chain_id"],
        "height": snap["height"],
        "snapshot_sha256": inventory["snapshot_sha256"],
        "validators_compared": len(inventory["rpc_endpoints"]),
        "policy_observation": "absent" if absent else "closed" if closed else "UNSAFE_OR_UNKNOWN",
        "enrollment_ready": False,
        "contract_count": len(contracts),
        "contracts": contracts,
        "blockers": [
            "Deployed source and executable IR need manual review and compiler parity checks.",
            "Upgrade/rehearse every legacy signature authority and audit persistent delegations.",
            "Repeat inventory on final release; check new deployments and governed patches.",
            "Independent security review, device recovery and release gates remain open.",
        ],
        "limitations": [
            "Trusted RPCs, no state proofs; endpoint count does not prove operator independence.",
            "Contract storage plus _accounts only; this is not a full chain backup.",
            "RPC JSON loses nested runtime types; use a typed storage export for rehearsal.",
            "Header app_hash commits H-1, not the queried H state.",
            "Static hints cannot prove safety. This tool never enables enrollment.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rpc", action="append", required=True)
    parser.add_argument("--chain-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # Fail rather than leaving stale success artifacts from an earlier run.
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    try:
        inventory = collect(args.rpc, args.chain_id)
        summary = report(inventory)
        (args.output / "inventory.json").write_text(json.dumps(inventory, indent=2) + "\n")
        (args.output / "report.json").write_text(json.dumps(summary, indent=2) + "\n")
    except Exception as exc:
        (args.output / "FAILED.json").write_text(
            json.dumps({"enrollment_ready": False, "error": str(exc)}) + "\n"
        )
        print(f"Audit failed: {exc}")
        return 1
    print(
        f"Captured {summary['contract_count']} contracts at height {summary['height']} from "
        f"{summary['validators_compared']} endpoints; policy {summary['policy_observation']}; "
        "enrollment NOT approved."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
