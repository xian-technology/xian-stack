from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import dex_automation_backend


class DexAutomationBackendTests(unittest.TestCase):
    def test_endpoints_use_loopback_display_for_public_host(self) -> None:
        endpoints = dex_automation_backend.dex_automation_endpoints(
            bind_host="0.0.0.0",
            port=38280,
        )

        self.assertEqual(
            "http://127.0.0.1:38280",
            endpoints["dex_automation"],
        )
        self.assertEqual(
            "http://127.0.0.1:38280/health",
            endpoints["dex_automation_health"],
        )

    def test_endpoints_bracket_ipv6_hosts(self) -> None:
        endpoints = dex_automation_backend.dex_automation_endpoints(
            bind_host="::1",
            port=38280,
        )

        self.assertEqual("http://[::1]:38280", endpoints["dex_automation"])
        self.assertEqual(
            "http://[::1]:38280/health",
            endpoints["dex_automation_health"],
        )

    def test_ensure_config_generates_wallet_and_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "xian-dex-automation"
            repo.mkdir()
            (repo / "pyproject.toml").write_text(
                "[project]\nname='xian-dex-automation'\n",
                encoding="utf-8",
            )
            process_dir = root / "artifacts"
            config_path = process_dir / "config.yaml"
            wallet_path = process_dir / "wallet.key"
            env = {
                "XIAN_DEX_AUTOMATION_DIR": str(repo),
                "XIAN_DEX_AUTOMATION_CONFIG": str(config_path),
                "XIAN_DEX_AUTOMATION_PRIVATE_KEY_FILE": str(wallet_path),
            }

            with patch.object(
                dex_automation_backend,
                "_PROCESS_DIR",
                process_dir,
            ):
                result = dex_automation_backend.ensure_dex_automation_config(
                    rpc_url="http://127.0.0.1:26657",
                    env=env,
                )

            self.assertEqual(
                str(repo.resolve()),
                result["dex_automation_repo_dir"],
            )
            self.assertTrue(config_path.exists())
            self.assertTrue(wallet_path.exists())
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(
                "http://127.0.0.1:26657",
                payload["network"]["rpc_url"],
            )
            self.assertEqual(
                str(wallet_path.resolve()),
                payload["wallet"]["private_key_file"],
            )

    def test_status_removes_stale_pid_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            process_dir = Path(temp_dir)
            pid_path = process_dir / "dex-automation.pid"
            log_path = process_dir / "dex-automation.log"
            pid_path.write_text("12345", encoding="utf-8")

            with patch.object(dex_automation_backend, "_PID_PATH", pid_path):
                with patch.object(dex_automation_backend, "_LOG_PATH", log_path):
                    with patch.object(
                        dex_automation_backend,
                        "_process_running",
                        return_value=False,
                    ):
                        status = dex_automation_backend.get_dex_automation_status(
                            bind_host="127.0.0.1",
                            port=38280,
                        )

            self.assertFalse(status["dex_automation_running"])
            self.assertIsNone(status["dex_automation_pid"])
            self.assertFalse(pid_path.exists())

    def test_start_cleans_up_pid_file_when_readiness_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            process_dir = Path(temp_dir)
            pid_path = process_dir / "dex-automation.pid"
            log_path = process_dir / "dex-automation.log"
            env = {
                "XIAN_DEX_AUTOMATION_DIR": str(process_dir),
                "XIAN_DEX_AUTOMATION_CONFIG": str(process_dir / "config.yaml"),
                "XIAN_DEX_AUTOMATION_PRIVATE_KEY_FILE": str(process_dir / "wallet.key"),
                "XIAN_PY_DIR": str(process_dir / "xian-py"),
            }

            with patch.object(dex_automation_backend, "_PROCESS_DIR", process_dir):
                with patch.object(dex_automation_backend, "_PID_PATH", pid_path):
                    with patch.object(dex_automation_backend, "_LOG_PATH", log_path):
                        with patch.object(
                            dex_automation_backend,
                            "ensure_dex_automation_config",
                            return_value={},
                        ):
                            with patch.object(
                                dex_automation_backend.subprocess,
                                "Popen",
                                return_value=SimpleNamespace(pid=12345),
                            ):
                                with patch.object(
                                    dex_automation_backend,
                                    "_process_running",
                                    return_value=False,
                                ):
                                    with patch.object(
                                        dex_automation_backend,
                                        "_wait_for_ready",
                                        side_effect=TimeoutError("port unavailable"),
                                    ):
                                        with self.assertRaisesRegex(
                                            TimeoutError,
                                            "port unavailable",
                                        ):
                                            dex_automation_backend.start_dex_automation_runtime(
                                                bind_host="127.0.0.1",
                                                port=38280,
                                                rpc_url="http://127.0.0.1:26657",
                                                env=env,
                                            )

            self.assertFalse(pid_path.exists())


if __name__ == "__main__":
    unittest.main()
