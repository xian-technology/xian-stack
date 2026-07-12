from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import backend


class BackendRuntimeTests(unittest.TestCase):
    def test_run_make_target_streams_output_to_stderr(self) -> None:
        completed = subprocess.CompletedProcess(["make", "node-start-bds"], 0)

        with patch("backend.subprocess.run", return_value=completed) as run_mock:
            result = backend.run_make_target(
                "node-start-bds",
                stream_output=True,
                env={"XIAN_BDS_ENABLED": "1"},
            )

        self.assertEqual(result, completed)
        args, kwargs = run_mock.call_args
        self.assertEqual(args[0], ["make", "node-start-bds"])
        self.assertIs(kwargs["stdout"], sys.stderr)
        self.assertIs(kwargs["stderr"], sys.stderr)
        self.assertNotIn("capture_output", kwargs)
        self.assertEqual(kwargs["env"], {"XIAN_BDS_ENABLED": "1"})

    def test_dex_bootstrap_backend_forwards_chi_budget_mode(self) -> None:
        completed = subprocess.CompletedProcess(
            ["python", "localnet-dex-bootstrap.py"],
            0,
            stdout='{"ok": true}',
            stderr="",
        )
        with patch("backend.run_python_script", return_value=completed) as run_mock:
            result = backend.backend_localnet_dex_bootstrap(
                deploy_helper=True,
                seed_demo_pool=False,
                top_up_liquidity=False,
                emit_test_swap=False,
                demo_token_contract="con_dex_demo_token",
                demo_lp_contract="con_dex_demo_lp",
                rpc_url="http://127.0.0.1:26657",
                chain_id="xian-localnet-1",
                deployer_private_key="private-key",
                dex_bundle="bundle.json",
                dex_contracts_dir=None,
                liquidity_currency_amount=10_000.0,
                liquidity_demo_token_amount=10_000.0,
                chi_budget_mode="fixed",
            )

        self.assertTrue(result["ok"])
        forwarded = run_mock.call_args.args[1:]
        mode_index = forwarded.index("--chi-budget-mode")
        self.assertEqual(forwarded[mode_index + 1], "fixed")


if __name__ == "__main__":
    unittest.main()
