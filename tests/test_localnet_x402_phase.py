from __future__ import annotations

import importlib.util
import json
import stat
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "localnet-e2e.py"
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("localnet_e2e", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"unable to load {MODULE_PATH}")
localnet_e2e = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = localnet_e2e
SPEC.loader.exec_module(localnet_e2e)

SMOKE_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "intentkit-x402-localnet-smoke.py"
)
SMOKE_SPEC = importlib.util.spec_from_file_location(
    "intentkit_x402_localnet_smoke",
    SMOKE_MODULE_PATH,
)
if SMOKE_SPEC is None or SMOKE_SPEC.loader is None:
    raise RuntimeError(f"unable to load {SMOKE_MODULE_PATH}")
intentkit_x402_smoke = importlib.util.module_from_spec(SMOKE_SPEC)
sys.modules[SMOKE_SPEC.name] = intentkit_x402_smoke
SMOKE_SPEC.loader.exec_module(intentkit_x402_smoke)


class LocalnetX402PhaseTests(unittest.TestCase):
    def test_x402_phase_is_part_of_five_node_e2e_sequence(self) -> None:
        phase_names = localnet_e2e.E2ERunner.phase_names()
        x402_index = phase_names.index("03-x402-exact")

        self.assertEqual("03-contract-orchestration", phase_names[x402_index - 1])
        self.assertEqual("03-intentkit-x402", phase_names[x402_index + 1])
        self.assertEqual("04-periodic-load", phase_names[x402_index + 2])

    def test_x402_phase_inputs_are_wired_for_runner_and_cli(self) -> None:
        self.assertTrue(localnet_e2e.X402_CONTRACT_SOURCE.exists())
        self.assertTrue(localnet_e2e.INTENTKIT_X402_SMOKE_SCRIPT.exists())
        self.assertEqual(Decimal("0.001"), localnet_e2e.X402_PAYMENT_AMOUNT)

        args = localnet_e2e.build_parser().parse_args(
            [
                "--start-phase",
                "03-x402-exact",
                "--resume-dir",
                "/tmp/localnet-e2e",
                "--no-bootstrap",
                "--no-build",
            ]
        )

        self.assertEqual("03-x402-exact", args.start_phase)
        self.assertEqual("/tmp/localnet-e2e", args.resume_dir)
        self.assertFalse(args.intentkit_x402)

    def test_intentkit_x402_phase_is_opt_in(self) -> None:
        args = localnet_e2e.build_parser().parse_args(
            [
                "--start-phase",
                "03-intentkit-x402",
                "--resume-dir",
                "/tmp/localnet-e2e",
                "--intentkit-x402",
                "--no-bootstrap",
                "--no-build",
            ]
        )

        self.assertEqual("03-intentkit-x402", args.start_phase)
        self.assertTrue(args.intentkit_x402)

    def test_parse_json_stdout_accepts_prefixed_logs(self) -> None:
        payload = localnet_e2e.parse_json_stdout(
            'log line\n{"timestamp": "noise"}\n{\n  "ok": true,\n  "value": 42\n}\n',
            label="test script",
        )

        self.assertEqual({"ok": True, "value": 42}, payload)

    def test_normalize_value_sorts_nested_dicts_for_stable_state_comparison(self) -> None:
        normalized = localnet_e2e.normalize_value(
            {"outer": {"b": Decimal("2"), "a": Decimal("1")}}
        )

        self.assertEqual(["outer"], list(normalized))
        self.assertEqual(["a", "b"], list(normalized["outer"]))

    def test_write_private_json_uses_owner_only_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "smoke-config.json"

            localnet_e2e.write_private_json(path, {"secret": "localnet-test-key"})

            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
            self.assertEqual({"secret": "localnet-test-key"}, json.loads(path.read_text()))

    def test_smoke_config_supplies_private_inputs_without_cli_args(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "smoke-config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "buyer_private_key": "buyer-test-key",
                        "chain_id": "xian-localnet-1",
                        "facilitator_private_key": "facilitator-test-key",
                        "rpc_url": "http://127.0.0.1:26657",
                        "run_id": "test-run",
                        "seller_private_key": "seller-test-key",
                        "settlement_contract": "con_x402_test",
                    }
                ),
                encoding="utf-8",
            )

            args = intentkit_x402_smoke.resolve_args(
                intentkit_x402_smoke.build_parser().parse_args(
                    ["--config", str(config_path)]
                )
            )

        self.assertEqual("buyer-test-key", args.buyer_private_key)
        self.assertEqual("http://127.0.0.1:26657", args.rpc_url)
        self.assertEqual(1, args.max_value)


if __name__ == "__main__":
    unittest.main()
