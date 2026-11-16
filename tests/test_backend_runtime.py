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

    def test_localnet_e2e_forwards_accounting_workload_size(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout='{"ok": true}', stderr="")
        with (
            patch("backend.run_python_script", return_value=completed) as run_mock,
            patch("backend.sys.stdout"),
        ):
            self.assertEqual(backend.main(["localnet-e2e", "--invariant-rounds", "120"]), 0)
        forwarded = run_mock.call_args.args[1:]
        self.assertEqual(forwarded[forwarded.index("--invariant-rounds") + 1], "120")

    def test_dex_bootstrap_backend_forwards_the_key_file_not_a_key(self) -> None:
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
                deployer_key_file="/secrets/deployer.key",
                dex_bundle="bundle.json",
                dex_contracts_dir=None,
                liquidity_currency_amount=10_000.0,
                liquidity_demo_token_amount=10_000.0,
            )

        self.assertTrue(result["ok"])
        forwarded = run_mock.call_args.args[1:]
        key_index = forwarded.index("--deployer-key-file")
        self.assertEqual(forwarded[key_index + 1], "/secrets/deployer.key")
        self.assertNotIn("--deployer-private-key", forwarded)
        self.assertNotIn("--chi-budget-mode", forwarded)

    def test_dex_bootstrap_forwards_each_given_option_and_nothing_else(self) -> None:
        completed = subprocess.CompletedProcess(["python"], 0, stdout='{"ok": true}', stderr="warn")
        with patch("backend.run_python_script", return_value=completed) as run_mock:
            result = backend.backend_localnet_dex_bootstrap(
                deploy_helper=False,
                seed_demo_pool=True,
                top_up_liquidity=True,
                emit_test_swap=False,
                demo_token_contract="con_t",
                demo_lp_contract="con_lp",
                rpc_url="http://node:26657",
                chain_id=None,
                deployer_key_file=None,
                dex_bundle="bundle.json",
                dex_contracts_dir="/dex",
                liquidity_currency_amount=1.0,
                liquidity_demo_token_amount=2.0,
            )

        self.assertEqual(result, {"ok": True, "stderr": "warn"})
        self.assertEqual(
            list(run_mock.call_args.args[1:]),
            [
                "--json-only",
                "--no-deploy-helper",
                "--seed-demo-pool",
                "--top-up-liquidity",
                "--no-emit-test-swap",
                "--demo-token-contract",
                "con_t",
                "--demo-lp-contract",
                "con_lp",
                "--liquidity-currency-amount",
                "1.0",
                "--liquidity-demo-token-amount",
                "2.0",
                "--dex-bundle",
                "bundle.json",
                "--dex-contracts-dir",
                "/dex",
                "--rpc-url",
                "http://node:26657",
            ],
        )

    def test_dex_bootstrap_script_failures_carry_the_script_output(self) -> None:
        failure = subprocess.CalledProcessError(
            1, ["python"], output="", stderr="no deployer key\n"
        )
        with (
            patch("backend.run_python_script", side_effect=failure),
            self.assertRaisesRegex(RuntimeError, "localnet-dex-bootstrap failed: no deployer key$"),
        ):
            backend.run_dex_bootstrap_script(["--json-only"])

        garbled = subprocess.CompletedProcess(["python"], 0, stdout="not json", stderr="")
        with (
            patch("backend.run_python_script", return_value=garbled),
            self.assertRaisesRegex(RuntimeError, "returned invalid JSON: not json"),
        ):
            backend.run_dex_bootstrap_script(["--json-only"])

    def test_dex_bootstrap_command_refuses_a_key_or_fixed_chi_on_the_command_line(self) -> None:
        for removed in (["--deployer-private-key", "ab" * 32], ["--chi-budget-mode", "fixed"]):
            with (
                self.subTest(removed=removed[0]),
                patch("backend.sys.stderr"),
                self.assertRaises(SystemExit),
            ):
                backend.main(["localnet-dex-bootstrap", *removed])


if __name__ == "__main__":
    unittest.main()
