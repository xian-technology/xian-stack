#!/usr/bin/env python3
"""Read public legacy state and rehearse a SAMPLE import; never creates launch genesis.

Run using the xian-abci environment. RPC is explicit, has no default, and only
/status and /abci_query reads are used. Legacy BDS has no height-pinned query,
so this evidence cannot be used as a finalized source snapshot.
"""

from __future__ import annotations

import argparse
import base64
import json
import urllib.parse
import urllib.request
from pathlib import Path

from xian.genesis_builder import build_bundle_network_genesis
from xian.genesis_migration import (
    ACCOUNT,
    amount_text,
    atoms,
    convert_legacy_state,
    decimal_text,
    digest,
    prepare_bundle,
    strict_json,
)
from xian_runtime_types.encoding import encode


def rehearse(rpc: str, configs: Path, output: Path, limit: int = 100):
    if not 1 <= limit <= 1000:
        raise ValueError("sample limit must be 1..1000")
    if output.exists():
        raise ValueError("output directory must not exist")
    output.mkdir(parents=True)

    def get(route, **params):
        query = "?" + urllib.parse.urlencode(params) if params else ""
        with urllib.request.urlopen(rpc.rstrip("/") + "/" + route + query, timeout=30) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError("RPC response exceeds sample bound")
        result = strict_json(raw)
        if "error" in result:
            raise ValueError("RPC returned an error")
        return raw, result["result"]

    before_raw, before = get("status")
    state_raw, state_result = get(
        "abci_query", path=json.dumps(f"/state/currency.balances/limit={limit}/offset=0")
    )
    after_raw, after = get("status")
    (output / "status-before.json").write_bytes(before_raw)
    (output / "status-after.json").write_bytes(after_raw)
    (output / "legacy-state-response.json").write_bytes(state_raw)
    response = state_result["response"]
    if response.get("code") != 0 or not response.get("value"):
        raise ValueError("legacy state query failed")
    rows = strict_json(base64.b64decode(response["value"], validate=True))
    (output / "legacy-state-rows.json").write_text(json.dumps(rows, indent=2) + "\n")
    balances, excluded = convert_legacy_state(rows)
    sample = [row for row in balances if ACCOUNT.fullmatch(row["account"])]
    omitted_contracts = [row for row in balances if not ACCOUNT.fullmatch(row["account"])]
    if not sample:
        raise ValueError("no account balances in sample")
    chain = before["node_info"]["network"]
    if after["node_info"]["network"] != chain:
        raise ValueError("source chain identity changed during capture")
    snapshot = {
        "schema": "xian.currency_snapshot.v1",
        "chain_id": chain,
        "height": int(before["sync_info"]["latest_block_height"]),
        "block_hash": before["sync_info"]["latest_block_hash"],
        "scope": "sample",
        "contracts": [],
        "total_supply": amount_text(sum(atoms(row["amount"]) for row in sample)),
        "balances": sample,
    }
    snapshot_raw = (json.dumps(snapshot, indent=2) + "\n").encode()
    (output / "sample-snapshot.json").write_bytes(snapshot_raw)
    plan = {
        "schema": "xian.balance_migration.v1",
        "source": {
            **{key: snapshot[key] for key in ("chain_id", "height", "block_hash")},
            "sha256": digest(snapshot_raw),
        },
        "target_chain_id": "balance-import-rehearsal",
        "contract_dispositions": {},
    }
    (output / "sample-plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    prepare_bundle(
        output / "sample-snapshot.json",
        output / "sample-plan.json",
        configs,
        "mainnet",
        output / "bundle",
        rehearsal=True,
    )
    genesis = build_bundle_network_genesis(
        chain_id=plan["target_chain_id"],
        network="mainnet",
        contracts_dir=output / "bundle",
        genesis_time="2026-01-01T00:00:00Z",
    )
    (output / "rehearsal-genesis.json").write_text(encode(genesis))
    state = {row["key"]: row["value"] for row in genesis["abci_genesis"]["genesis"]}
    for row in sample:
        assert atoms(decimal_text(str(state["currency.balances:" + row["account"]]))) == atoms(
            row["amount"]
        )
    assert state["_accounts.policy"]["enrollment_open"] is False
    report = {
        "schema": "xian.balance_import_rehearsal.v1",
        "rpc": rpc,
        "observed_chain_id": chain,
        "observed_height_before": snapshot["height"],
        "observed_height_after": int(after["sync_info"]["latest_block_height"]),
        "height_pinned_snapshot": False,
        "scope": "sample",
        "launch_approved": False,
        "enrollment_open": False,
        "rows_captured": len(rows),
        "accounts_imported": len(sample),
        "sample_total": snapshot["total_supply"],
        "excluded_non_balance_rows": excluded,
        "contract_holdings_not_selected_for_sample": omitted_contracts,
        "state_comparison_passed": True,
        "snapshot_sha256": digest(snapshot_raw),
        "genesis_sha256": digest((output / "rehearsal-genesis.json").read_bytes()),
        "limitations": [
            "Live legacy BDS returns height zero and does not pin queries to a block.",
            "This sample does not establish full supply, consensus or contract beneficiaries.",
            "No public-chain transaction was sent; access uses separate disposable-key tests.",
        ],
    }
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rpc", required=True)
    parser.add_argument("--configs", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    report = rehearse(args.rpc, args.configs, args.output, args.limit)
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "observed_chain_id",
                    "accounts_imported",
                    "sample_total",
                    "state_comparison_passed",
                    "launch_approved",
                )
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
