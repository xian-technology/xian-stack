from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import backend


class BackendRequestTests(unittest.TestCase):
    def test_main_accepts_json_backend_request(self) -> None:
        request = {
            "schema_version": 1,
            "command": "status",
            "options": {
                "bds_enabled": True,
                "dashboard": True,
                "dashboard_host": "0.0.0.0",
                "dashboard_port": 18080,
            },
        }
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as handle:
            json.dump(request, handle)
            handle.flush()

            with patch.object(
                backend,
                "backend_status",
                return_value={"ok": True},
            ) as status:
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    exit_code = backend.main(["--request-json", handle.name])

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), {"ok": True})
        status.assert_called_once()
        self.assertTrue(status.call_args.kwargs["bds_enabled"])
        self.assertTrue(status.call_args.kwargs["dashboard_enabled"])
        self.assertEqual(status.call_args.kwargs["dashboard_host"], "0.0.0.0")
        self.assertEqual(status.call_args.kwargs["dashboard_port"], 18080)

    def test_localnet_init_passes_chain_id_to_script(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="generated\n",
            stderr="",
        )
        with patch.object(
            backend,
            "run_python_script",
            return_value=completed,
        ) as run_script:
            with patch.object(
                backend,
                "load_localnet_metadata",
                return_value={"chain_id": "metadata-chain"},
            ):
                result = backend.backend_localnet_init(
                    nodes=3,
                    clean=True,
                    topology="integrated",
                    genesis_network="devnet",
                    chain_id="custom-chain",
                )

        args = run_script.call_args.args
        self.assertEqual(args[0], backend.LOCALNET_INIT_SCRIPT)
        self.assertIn("--chain-id", args)
        chain_id_index = args.index("--chain-id")
        self.assertEqual(args[chain_id_index + 1], "custom-chain")
        self.assertIn("--clean", args)
        self.assertEqual(result["chain_id"], "custom-chain")
        self.assertEqual(result["stdout"], "generated\n")


if __name__ == "__main__":
    unittest.main()
