from __future__ import annotations

import importlib.util
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


def _load_localnet_init_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "localnet-init.py"
    spec = importlib.util.spec_from_file_location("localnet_init", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class BackendRequestTests(unittest.TestCase):
    def _run_json_request(self, request: dict) -> tuple[int, str]:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as handle:
            json.dump(request, handle)
            handle.flush()

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = backend.main(["--request-json", handle.name])
        return exit_code, stdout.getvalue()

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
        with patch.object(
            backend,
            "backend_status",
            return_value={"ok": True},
        ) as status:
            exit_code, stdout = self._run_json_request(request)

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout), {"ok": True})
        status.assert_called_once()
        self.assertTrue(status.call_args.kwargs["bds_enabled"])
        self.assertTrue(status.call_args.kwargs["dashboard_enabled"])
        self.assertEqual(status.call_args.kwargs["dashboard_host"], "0.0.0.0")
        self.assertEqual(status.call_args.kwargs["dashboard_port"], 18080)

    def test_main_rejects_unknown_json_backend_option(self) -> None:
        request = {
            "schema_version": 1,
            "command": "status",
            "options": {"unknown_option": "accepted-before"},
        }

        with self.assertRaisesRegex(ValueError, "unknown_option"):
            self._run_json_request(request)

    def test_main_rejects_invalid_json_backend_choice(self) -> None:
        request = {
            "schema_version": 1,
            "command": "status",
            "options": {"node_image_mode": "bogus"},
        }

        with self.assertRaisesRegex(ValueError, "node_image_mode must be one of"):
            self._run_json_request(request)

    def test_main_rejects_invalid_json_backend_type(self) -> None:
        request = {
            "schema_version": 1,
            "command": "status",
            "options": {"dashboard_port": "not-an-int"},
        }

        with self.assertRaisesRegex(ValueError, "dashboard_port must be int"):
            self._run_json_request(request)

    def test_backend_health_checks_grafana_only_when_monitoring_is_enabled(self) -> None:
        def fake_backend_status(**_: object) -> dict:
            return {
                "backend_running": True,
                "node_id": "node-1",
                "endpoints": {
                    "cometbft_metrics": "http://metrics.local",
                    "xian_metrics": "http://app-metrics.local",
                    "prometheus": "http://prometheus.local",
                    "grafana": "http://grafana.local",
                    "bds_status_query": "http://rpc.local/bds-status",
                    "graphiql": "http://graphiql.local",
                    "intentkit": "http://intentkit.local",
                    "intentkit_api": "http://intentkit-api.local",
                },
                "prometheus_reachable": True,
                "grafana_reachable": True,
                "graphiql_reachable": True,
                "intentkit_reachable": True,
                "intentkit_api_reachable": True,
            }

        base_kwargs = {
            "node_image_mode": "local_build",
            "node_integrated_image": None,
            "node_split_image": None,
            "bds_enabled": False,
            "dashboard_enabled": False,
            "dashboard_host": "127.0.0.1",
            "dashboard_port": 8080,
            "public_rpc_enabled": False,
            "public_query_enabled": False,
            "public_metrics_enabled": False,
            "intentkit_network_id": "xian-localnet",
            "intentkit_host": "127.0.0.1",
            "intentkit_port": 38000,
            "intentkit_api_port": 38080,
            "dex_automation_enabled": False,
            "dex_automation_host": "127.0.0.1",
            "dex_automation_port": 38280,
            "dex_automation_config": None,
            "shielded_relayer_enabled": False,
            "shielded_relayer_host": "127.0.0.1",
            "shielded_relayer_port": 38180,
            "rpc_url": "http://rpc.local/status",
            "check_disk": False,
        }
        with (
            patch.object(backend, "backend_status", side_effect=fake_backend_status),
            patch.object(
                backend,
                "fetch_json",
                return_value={"result": {"sync_info": {}, "node_info": {}}},
            ),
            patch.object(
                backend,
                "fetch_abci_query_value",
                return_value={
                    "db_status": "ok",
                    "worker_running": True,
                    "alerts": [],
                    "indexed": {},
                },
            ),
            patch.object(backend, "probe_http_endpoint", return_value={"ok": True}),
        ):
            monitoring_result = backend.backend_health(
                **base_kwargs,
                monitoring_enabled=True,
                intentkit_enabled=False,
            )
            intentkit_result = backend.backend_health(
                **base_kwargs,
                monitoring_enabled=False,
                intentkit_enabled=True,
            )
            bds_result = backend.backend_health(
                **{**base_kwargs, "bds_enabled": True},
                monitoring_enabled=False,
                intentkit_enabled=False,
            )

        self.assertIn("grafana", monitoring_result["checks"])
        self.assertNotIn("grafana", intentkit_result["checks"])
        self.assertEqual(intentkit_result["state"], "healthy")
        self.assertTrue(bds_result["checks"]["bds"]["ok"])
        self.assertTrue(bds_result["checks"]["graphiql"]["ok"])

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

    def test_localnet_bds_rpc_url_matches_runtime_topology(self) -> None:
        localnet_init = _load_localnet_init_module()
        node = {"moniker": "node-1"}

        self.assertEqual(
            localnet_init.bds_runtime_rpc_url(node, "integrated"),
            "http://127.0.0.1:26657",
        )
        self.assertEqual(
            localnet_init.bds_runtime_rpc_url(node, "fidelity"),
            "http://node-1:26657",
        )


if __name__ == "__main__":
    unittest.main()
