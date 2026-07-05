from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import backend


def runtime_env_kwargs(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
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
        "intentkit_enabled": False,
        "intentkit_network_id": "xian-localnet",
        "intentkit_host": "127.0.0.1",
        "intentkit_port": 38000,
        "intentkit_api_port": 38080,
        "dex_automation_enabled": False,
        "dex_automation_host": backend.DEFAULT_DEX_AUTOMATION_HOST,
        "dex_automation_port": backend.DEFAULT_DEX_AUTOMATION_PORT,
        "dex_automation_config": None,
        "shielded_relayer_enabled": False,
        "shielded_relayer_host": backend.DEFAULT_SHIELDED_RELAYER_HOST,
        "shielded_relayer_port": backend.DEFAULT_SHIELDED_RELAYER_PORT,
    }
    kwargs.update(overrides)
    return kwargs


class BackendSecurityDefaultsTests(unittest.TestCase):
    def test_runtime_env_generates_local_secrets_and_safe_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stack_dir = Path(temp_dir) / "xian-stack"
            stack_dir.mkdir()
            with patch.object(backend, "STACK_DIR", stack_dir):
                with patch.dict(os.environ, {}, clear=True):
                    env = backend.runtime_env(**runtime_env_kwargs())

            secrets_env = stack_dir / ".stack-secrets.env"
            self.assertTrue(secrets_env.exists())
            self.assertEqual(str(secrets_env.resolve()), env["XIAN_STACK_SECRETS_ENV"])
            self.assertTrue(env["XIAN_BDS_PASSWORD"])
            self.assertTrue(env["XIAN_POSTGRAPHILE_PASSWORD"])
            self.assertEqual("127.0.0.1", env["XIAN_COMETBFT_RPC_HOST"])
            self.assertEqual("127.0.0.1", env["XIAN_COMETBFT_METRICS_HOST"])
            self.assertEqual("127.0.0.1", env["XIAN_APP_METRICS_HOST"])
            self.assertEqual("127.0.0.1", env["XIAN_POSTGRAPHILE_HOST"])
            self.assertEqual("0", env["XIAN_PUBLIC_RPC_ENABLED"])
            self.assertEqual("0", env["XIAN_PUBLIC_QUERY_ENABLED"])
            self.assertEqual("0", env["XIAN_PUBLIC_METRICS_ENABLED"])

    def test_runtime_env_offsets_hidden_intentkit_s3_port_on_collision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stack_dir = Path(temp_dir) / "xian-stack"
            stack_dir.mkdir()
            with patch.object(backend, "STACK_DIR", stack_dir):
                with patch.dict(os.environ, {}, clear=True):
                    env = backend.runtime_env(
                        **runtime_env_kwargs(
                            intentkit_enabled=True,
                            intentkit_port=39000,
                            intentkit_api_port=39001,
                        )
                    )

            self.assertEqual("39000", env["XIAN_INTENTKIT_PORT"])
            self.assertEqual("39001", env["XIAN_INTENTKIT_API_PORT"])
            self.assertEqual("39002", env["XIAN_INTENTKIT_S3_PORT"])

    def test_runtime_env_preserves_explicit_intentkit_s3_port(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stack_dir = Path(temp_dir) / "xian-stack"
            stack_dir.mkdir()
            with patch.object(backend, "STACK_DIR", stack_dir):
                with patch.dict(
                    os.environ,
                    {"XIAN_INTENTKIT_S3_PORT": "39123"},
                    clear=True,
                ):
                    env = backend.runtime_env(
                        **runtime_env_kwargs(
                            intentkit_enabled=True,
                            intentkit_port=39000,
                        )
                    )

            self.assertEqual("39123", env["XIAN_INTENTKIT_S3_PORT"])

    def test_runtime_env_rejects_weak_default_bds_password(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stack_dir = Path(temp_dir) / "xian-stack"
            stack_dir.mkdir()
            with patch.object(backend, "STACK_DIR", stack_dir):
                with patch.dict(
                    os.environ,
                    {
                        "XIAN_BDS_PASSWORD": "xian",
                        "XIAN_POSTGRAPHILE_PASSWORD": "strong-postgraphile-secret",
                    },
                    clear=True,
                ):
                    with self.assertRaisesRegex(ValueError, "weak default value 'xian'"):
                        backend.runtime_env(**runtime_env_kwargs())

    def test_runtime_env_rejects_public_rpc_without_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stack_dir = Path(temp_dir) / "xian-stack"
            stack_dir.mkdir()
            with patch.object(backend, "STACK_DIR", stack_dir):
                with patch.dict(
                    os.environ,
                    {"XIAN_COMETBFT_RPC_HOST": "0.0.0.0"},
                    clear=True,
                ):
                    with self.assertRaisesRegex(
                        ValueError,
                        "Public RPC exposure requires XIAN_PUBLIC_RPC_ENABLED=1",
                    ):
                        backend.runtime_env(**runtime_env_kwargs())

    def test_runtime_env_public_query_requires_bds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stack_dir = Path(temp_dir) / "xian-stack"
            stack_dir.mkdir()
            with patch.object(backend, "STACK_DIR", stack_dir):
                with patch.dict(
                    os.environ,
                    {"XIAN_POSTGRAPHILE_HOST": "0.0.0.0"},
                    clear=True,
                ):
                    with self.assertRaisesRegex(
                        ValueError,
                        "Public PostGraphile exposure requires XIAN_BDS_ENABLED=1",
                    ):
                        backend.runtime_env(**runtime_env_kwargs(public_query_enabled=True))

    def test_runtime_env_exposes_only_requested_surfaces(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stack_dir = Path(temp_dir) / "xian-stack"
            stack_dir.mkdir()
            with patch.object(backend, "STACK_DIR", stack_dir):
                with patch.dict(os.environ, {}, clear=True):
                    env = backend.runtime_env(
                        **runtime_env_kwargs(
                            bds_enabled=True,
                            public_query_enabled=True,
                        )
                    )

            self.assertEqual("127.0.0.1", env["XIAN_COMETBFT_RPC_HOST"])
            self.assertEqual("127.0.0.1", env["XIAN_COMETBFT_METRICS_HOST"])
            self.assertEqual("127.0.0.1", env["XIAN_APP_METRICS_HOST"])
            self.assertEqual("0.0.0.0", env["XIAN_POSTGRAPHILE_HOST"])

    def test_runtime_env_rejects_public_dex_automation_without_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stack_dir = Path(temp_dir) / "xian-stack"
            stack_dir.mkdir()
            with patch.object(backend, "STACK_DIR", stack_dir):
                with patch.dict(os.environ, {}, clear=True):
                    with self.assertRaisesRegex(
                        ValueError,
                        "Public DEX automation exposure requires XIAN_PUBLIC_QUERY_ENABLED=1",
                    ):
                        backend.runtime_env(
                            **runtime_env_kwargs(
                                dex_automation_enabled=True,
                                dex_automation_host="0.0.0.0",
                            )
                        )

    def test_runtime_env_rejects_public_monitoring_without_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stack_dir = Path(temp_dir) / "xian-stack"
            stack_dir.mkdir()
            with patch.object(backend, "STACK_DIR", stack_dir):
                with patch.dict(
                    os.environ,
                    {"XIAN_PROMETHEUS_HOST": "0.0.0.0"},
                    clear=True,
                ):
                    with self.assertRaisesRegex(
                        ValueError,
                        "Public monitoring exposure requires XIAN_PUBLIC_MONITORING_ENABLED=1",
                    ):
                        backend.runtime_env(**runtime_env_kwargs())

    def test_runtime_env_rejects_public_monitoring_without_auth_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stack_dir = Path(temp_dir) / "xian-stack"
            stack_dir.mkdir()
            with patch.object(backend, "STACK_DIR", stack_dir):
                with patch.dict(
                    os.environ,
                    {
                        "XIAN_GRAFANA_HOST": "0.0.0.0",
                        "XIAN_PUBLIC_MONITORING_ENABLED": "1",
                    },
                    clear=True,
                ):
                    with self.assertRaisesRegex(
                        ValueError,
                        "XIAN_MONITORING_PUBLIC_AUTH_CONFIRMED=1",
                    ):
                        backend.runtime_env(**runtime_env_kwargs())

    def test_runtime_env_allows_public_monitoring_with_explicit_auth_confirmation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stack_dir = Path(temp_dir) / "xian-stack"
            stack_dir.mkdir()
            with patch.object(backend, "STACK_DIR", stack_dir):
                with patch.dict(
                    os.environ,
                    {
                        "XIAN_PROMETHEUS_HOST": "0.0.0.0",
                        "XIAN_GRAFANA_HOST": "0.0.0.0",
                        "XIAN_PUBLIC_MONITORING_ENABLED": "1",
                        "XIAN_MONITORING_PUBLIC_AUTH_CONFIRMED": "1",
                    },
                    clear=True,
                ):
                    env = backend.runtime_env(**runtime_env_kwargs())

        self.assertEqual("0.0.0.0", env["XIAN_PROMETHEUS_HOST"])
        self.assertEqual("0.0.0.0", env["XIAN_GRAFANA_HOST"])

    def test_monitoring_compose_does_not_enable_prometheus_lifecycle(self) -> None:
        compose = (Path(__file__).resolve().parents[1] / "docker-compose-monitoring.yml")

        self.assertNotIn("--web.enable-lifecycle", compose.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
