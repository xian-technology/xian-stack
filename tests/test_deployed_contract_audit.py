import base64
import importlib.util
import json
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "audit", Path(__file__).resolve().parents[1] / "scripts" / "deployed_contract_audit.py"
)
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


def wire(value, info="dict", height=12):
    text = json.dumps(value) if info in {"dict", "list"} else value
    return {
        "height": height,
        "info": info,
        "value": None if text is None else base64.b64encode(text.encode()).decode(),
    }


class FakeRpc:
    def __init__(self, pages):
        self.pages = iter(pages)

    def query(self, path, height=0):
        return wire(next(self.pages))


class AuditTests(unittest.TestCase):
    def test_truncated_contract_inventory_is_rejected(self):
        rpc = FakeRpc(
            [
                {"total": 2, "offset": 0, "items": [{"name": "currency"}]},
                {"total": 2, "offset": 1, "items": []},
            ]
        )
        with self.assertRaises(audit.AuditError):
            audit.contract_names(rpc)

    def test_duplicate_and_changing_contract_inventory_are_rejected(self):
        for total, name in [(2, "currency"), (3, "other")]:
            with self.subTest(total=total):
                rpc = FakeRpc(
                    [
                        {"total": 2, "offset": 0, "items": [{"name": "currency"}]},
                        {"total": total, "offset": 1, "items": [{"name": name}]},
                    ]
                )
                with self.assertRaises(audit.AuditError):
                    audit.contract_names(rpc)

    def test_storage_pagination_cannot_silently_skip_or_repeat(self):
        page = {
            "prefix": "currency.",
            "items": ["balances:a"],
            "has_more": True,
            "next_after": "balances:a",
        }
        for bad in [dict(page), {**page, "items": []}, {**page, "next_after": "balances:b"}]:
            with self.subTest(bad=bad), self.assertRaises(audit.AuditError):
                audit.storage_keys(FakeRpc([page, bad]), "currency.", 12)

    def test_storage_final_cursor_and_unsafe_path_fail(self):
        for items, cursor in [(["value"], "value"), (["bad/key"], None)]:
            rpc = FakeRpc(
                [{"prefix": "currency.", "items": items, "has_more": False, "next_after": cursor}]
            )
            with self.assertRaises(audit.AuditError):
                audit.storage_keys(rpc, "currency.", 12)

    def test_wire_types_remain_distinct(self):
        self.assertNotEqual(audit.digest(wire("12", "str")), audit.digest(wire("12", "decimal")))
        self.assertIsNone(audit.decoded(wire(None, "")))
        self.assertEqual(audit.decoded(wire(None, "str")), "")
        self.assertIsNone(audit.decoded(wire("\0", "")))
        with self.assertRaises(Exception):
            audit.decoded({"info": "str", "value": "!!!"})

    def test_wrong_height_and_missing_value_fail(self):
        responses = [
            {"code": 0, "height": "13", "value": None},
            {"code": 0, "height": "12"},
            {"code": 1, "height": "12"},
        ]
        for response in responses:
            rpc = audit.Rpc("http://127.0.0.1:26657")
            with patch.object(rpc, "get", return_value={"response": response}):
                with self.assertRaises(audit.AuditError):
                    rpc.query("get/_accounts.policy", 12)

    def test_disagreeing_validators_fail(self):
        first = {"height": 12, "state": {"currency.balance": "1"}}
        second = {"height": 12, "state": {"currency.balance": "2"}}
        with patch.object(audit, "capture", side_effect=[first, second]):
            with self.assertRaises(audit.AuditError):
                audit.collect(["http://127.0.0.1:1", "http://127.0.0.1:2"], "local")
        with self.assertRaises(audit.AuditError):
            audit.collect(["http://127.0.0.1:1", "http://127.0.0.1:1/"], "local")

    def test_hints_cannot_approve_even_clean_source(self):
        snapshot = {
            "chain_id": "local",
            "height": 12,
            "contracts": [{"name": "currency"}],
            "state": {"currency.__source__": wire("@export\ndef get():\n    return 1\n", "str")},
            "policy": wire(None, ""),
        }
        cases = [
            (None, "absent"),
            ({"enrollment_open": False}, "closed"),
            ({"enrollment_open": True}, "UNSAFE_OR_UNKNOWN"),
        ]
        for policy, expected in cases:
            snap = deepcopy(snapshot)
            if policy is not None:
                snap["policy"] = wire(policy)
                snap["state"]["_accounts.policy"] = snap["policy"]
            result = audit.report(
                {"snapshot": snap, "snapshot_sha256": "abc", "rpc_endpoints": ["a", "b"]}
            )
            self.assertFalse(result["enrollment_ready"])
            self.assertEqual(result["policy_observation"], expected)
            self.assertEqual(result["contracts"][0]["review_status"], "manual_review_required")
        hints = audit.review_hints("c = crypto\nf = c.verify\nresult = f(owner, msg, sig)")
        self.assertTrue(hints["signature_review"])

    def test_capture_requires_source_and_ir(self):
        rpc = audit.Rpc("http://127.0.0.1:1")
        with (
            patch.object(
                rpc,
                "get",
                return_value={
                    "node_info": {"network": "local"},
                    "sync_info": {"catching_up": False},
                },
            ),
            patch.object(audit, "contract_names", return_value=(12, ["currency"])),
            patch.object(audit, "storage_keys", return_value=[]),
        ):
            with self.assertRaisesRegex(audit.AuditError, "Missing source or executable IR"):
                audit.capture(rpc, "local")


if __name__ == "__main__":
    unittest.main()
