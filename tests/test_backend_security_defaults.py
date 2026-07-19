from __future__ import annotations

import importlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

backend = importlib.import_module("backend")


PINNED_POSTGRES_IMAGE = "postgres:17.10@sha256:" + "a" * 64
PINNED_PROMETHEUS_IMAGE = "prom/prometheus:v3.12.0@sha256:" + "b" * 64
PINNED_GRAFANA_IMAGE = "grafana/grafana:12.4.5@sha256:" + "c" * 64


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
            self.assertEqual("10000", env["XIAN_POSTGRAPHILE_STATEMENT_TIMEOUT_MS"])
            self.assertEqual("1048576", env["XIAN_POSTGRAPHILE_BODY_SIZE_LIMIT_BYTES"])
            self.assertEqual("1", env["XIAN_POSTGRAPHILE_DISABLE_DEFAULT_MUTATIONS"])
            self.assertEqual("omit", env["XIAN_POSTGRAPHILE_SIMPLE_COLLECTIONS"])
            self.assertEqual("60", env["XIAN_POSTGRAPHILE_SCHEMA_WAIT_TIMEOUT_SECONDS"])
            self.assertEqual(
                "addresses,bds_meta,blocks,contracts,events,rewards,shielded_output_tags,"
                "shielded_outputs,state,state_changes,state_patches,transactions",
                env["XIAN_POSTGRAPHILE_REQUIRED_TABLES"],
            )
            self.assertEqual("0", env["XIAN_PUBLIC_RPC_ENABLED"])
            self.assertEqual("0", env["XIAN_PUBLIC_QUERY_ENABLED"])
            self.assertEqual("0", env["XIAN_PUBLIC_METRICS_ENABLED"])
            self.assertEqual("0", env["XIAN_REQUIRE_DIGEST_PINNED_THIRD_PARTY_IMAGES"])
            self.assertEqual("postgres:17.10", env["XIAN_POSTGRES_IMAGE"])
            self.assertEqual("prom/prometheus:v3.12.0", env["XIAN_PROMETHEUS_IMAGE"])
            self.assertEqual("grafana/grafana:12.4.5", env["XIAN_GRAFANA_IMAGE"])

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

    def test_runtime_env_preserves_explicit_ipv6_public_binds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stack_dir = Path(temp_dir) / "xian-stack"
            stack_dir.mkdir()
            with patch.object(backend, "STACK_DIR", stack_dir):
                with patch.dict(
                    os.environ,
                    {
                        "XIAN_COMETBFT_RPC_HOST": "::",
                        "XIAN_COMETBFT_METRICS_HOST": "::",
                        "XIAN_APP_METRICS_HOST": "::",
                    },
                    clear=True,
                ):
                    env = backend.runtime_env(
                        **runtime_env_kwargs(
                            public_rpc_enabled=True,
                            public_metrics_enabled=True,
                        )
                    )

        self.assertEqual("::", env["XIAN_COMETBFT_RPC_HOST"])
        self.assertEqual("::", env["XIAN_COMETBFT_METRICS_HOST"])
        self.assertEqual("::", env["XIAN_APP_METRICS_HOST"])

    def test_runtime_env_endpoints_bracket_ipv6_hosts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stack_dir = Path(temp_dir) / "xian-stack"
            stack_dir.mkdir()
            with patch.object(backend, "STACK_DIR", stack_dir):
                with patch.dict(
                    os.environ,
                    {
                        "XIAN_COMETBFT_RPC_HOST": "::1",
                        "XIAN_COMETBFT_METRICS_HOST": "::1",
                        "XIAN_APP_METRICS_HOST": "::1",
                        "XIAN_POSTGRAPHILE_HOST": "::1",
                        "XIAN_PROMETHEUS_HOST": "::1",
                        "XIAN_GRAFANA_HOST": "::1",
                    },
                    clear=True,
                ):
                    with patch.object(
                        backend,
                        "_discover_runtime_endpoints",
                        return_value={},
                    ):
                        result = backend.backend_endpoints(
                            **runtime_env_kwargs(
                                bds_enabled=True,
                                dashboard_enabled=True,
                                dashboard_host="::1",
                                monitoring_enabled=True,
                                intentkit_enabled=True,
                                intentkit_host="::1",
                                dex_automation_enabled=True,
                                dex_automation_host="::1",
                                shielded_relayer_enabled=True,
                                shielded_relayer_host="::1",
                            )
                        )

        endpoints = result["endpoints"]
        self.assertEqual("http://[::1]:26657", endpoints["rpc"])
        self.assertEqual("http://[::1]:26657/status", endpoints["rpc_status"])
        self.assertEqual("http://[::1]:26660/metrics", endpoints["cometbft_metrics"])
        self.assertEqual("http://[::1]:9108/metrics", endpoints["xian_metrics"])
        self.assertEqual("http://[::1]:5000/graphql", endpoints["graphql"])
        self.assertEqual("http://[::1]:5000/graphiql", endpoints["graphiql"])
        self.assertEqual("http://[::1]:8080", endpoints["dashboard"])
        self.assertEqual("http://[::1]:9090", endpoints["prometheus"])
        self.assertEqual("http://[::1]:3000", endpoints["grafana"])
        self.assertEqual("http://[::1]:38000", endpoints["intentkit"])
        self.assertEqual("http://[::1]:39000/static", endpoints["intentkit_static"])
        self.assertEqual("http://[::1]:38280", endpoints["dex_automation"])
        self.assertEqual("http://[::1]:38180", endpoints["shielded_relayer"])
        self.assertTrue(endpoints["bds_status_query"].startswith("http://[::1]:26657/"))

    def test_default_runtime_rpc_status_url_follows_ipv6_rpc_bind(self) -> None:
        env = {
            "XIAN_COMETBFT_RPC_HOST": "::1",
            "XIAN_COMETBFT_RPC_PORT": "26657",
        }

        self.assertEqual(
            backend.runtime_rpc_status_url(env, backend.DEFAULT_RPC_STATUS_URL),
            "http://[::1]:26657/status",
        )

    def test_custom_runtime_rpc_status_url_is_preserved(self) -> None:
        env = {
            "XIAN_COMETBFT_RPC_HOST": "::1",
            "XIAN_COMETBFT_RPC_PORT": "26657",
        }

        self.assertEqual(
            backend.runtime_rpc_status_url(env, "http://rpc.example:26657/status"),
            "http://rpc.example:26657/status",
        )

    def test_runtime_env_allows_public_dashboard_host_when_dashboard_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stack_dir = Path(temp_dir) / "xian-stack"
            stack_dir.mkdir()
            with patch.object(backend, "STACK_DIR", stack_dir):
                with patch.dict(os.environ, {}, clear=True):
                    env = backend.runtime_env(
                        **runtime_env_kwargs(
                            dashboard_enabled=False,
                            dashboard_host="0.0.0.0",
                        )
                    )

            self.assertEqual("0", env["XIAN_DASHBOARD_ENABLED"])
            self.assertEqual("0.0.0.0", env["XIAN_DASHBOARD_HOST"])
            self.assertEqual("0", env["XIAN_PUBLIC_QUERY_ENABLED"])

    def test_runtime_env_rejects_public_dashboard_without_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stack_dir = Path(temp_dir) / "xian-stack"
            stack_dir.mkdir()
            with patch.object(backend, "STACK_DIR", stack_dir):
                with patch.dict(os.environ, {}, clear=True):
                    with self.assertRaisesRegex(
                        ValueError,
                        "Public dashboard exposure requires XIAN_PUBLIC_QUERY_ENABLED=1",
                    ):
                        backend.runtime_env(
                            **runtime_env_kwargs(
                                dashboard_enabled=True,
                                dashboard_host="0.0.0.0",
                            )
                        )

    def test_shell_security_allows_public_dashboard_host_when_dashboard_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env = {
                **os.environ,
                "XIAN_STACK_SECRETS_ENV": str(Path(temp_dir) / "secrets.env"),
                "XIAN_DASHBOARD_ENABLED": "0",
                "XIAN_DASHBOARD_HOST": "0.0.0.0",
                "XIAN_PUBLIC_QUERY_ENABLED": "0",
            }
            result = subprocess.run(
                ["bash", "-lc", "source ./scripts/stack-env.sh && export_stack_env >/dev/null"],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_shell_security_rejects_public_dashboard_without_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env = {
                **os.environ,
                "XIAN_STACK_SECRETS_ENV": str(Path(temp_dir) / "secrets.env"),
                "XIAN_DASHBOARD_ENABLED": "1",
                "XIAN_DASHBOARD_HOST": "0.0.0.0",
                "XIAN_PUBLIC_QUERY_ENABLED": "0",
            }
            result = subprocess.run(
                ["bash", "-lc", "source ./scripts/stack-env.sh && export_stack_env >/dev/null"],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "Public dashboard exposure requires XIAN_PUBLIC_QUERY_ENABLED=1",
            result.stderr,
        )

    def test_shell_security_treats_bracketed_ipv6_loopback_as_loopback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env = {
                **os.environ,
                "XIAN_STACK_SECRETS_ENV": str(Path(temp_dir) / "secrets.env"),
                "XIAN_COMETBFT_RPC_HOST": "[::1]",
            }
            result = subprocess.run(
                ["bash", "-lc", "source ./scripts/stack-env.sh && export_stack_env >/dev/null"],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_shell_security_rejects_non_loopback_ipv6_without_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env = {
                **os.environ,
                "XIAN_STACK_SECRETS_ENV": str(Path(temp_dir) / "secrets.env"),
                "XIAN_COMETBFT_RPC_HOST": "2001:db8::1",
            }
            result = subprocess.run(
                ["bash", "-lc", "source ./scripts/stack-env.sh && export_stack_env >/dev/null"],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "Public RPC exposure requires XIAN_PUBLIC_RPC_ENABLED=1",
            result.stderr,
        )

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
        compose = Path(__file__).resolve().parents[1] / "docker-compose-monitoring.yml"

        self.assertNotIn("--web.enable-lifecycle", compose.read_text(encoding="utf-8"))

    def test_bds_compose_uses_configurable_postgres_image(self) -> None:
        compose = Path(__file__).resolve().parents[1] / "docker-compose-abci-bds.yml"
        source = compose.read_text(encoding="utf-8")

        self.assertIn("image: ${XIAN_POSTGRES_IMAGE:-postgres:17.10}", source)
        self.assertNotIn("image: postgres:17.10", source)

    def test_compose_uses_long_form_host_ip_for_configurable_port_binds(self) -> None:
        root = Path(__file__).resolve().parents[1]
        compose_sources = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (
                root / "docker-compose-abci.yml",
                root / "docker-compose-abci-bds.yml",
                root / "docker-compose-monitoring.yml",
                root / "docker-compose-intentkit.yml",
            )
        )

        self.assertNotRegex(compose_sources, r'"\$\{XIAN_[A-Z0-9_]*HOST\}:\$\{')
        for host_var in (
            "XIAN_COMETBFT_RPC_HOST",
            "XIAN_COMETBFT_P2P_HOST",
            "XIAN_COMETBFT_METRICS_HOST",
            "XIAN_APP_METRICS_HOST",
            "XIAN_DASHBOARD_HOST",
            "XIAN_POSTGRAPHILE_HOST",
            "XIAN_PROMETHEUS_HOST",
            "XIAN_GRAFANA_HOST",
            "XIAN_INTENTKIT_HOST",
        ):
            self.assertIn(f'host_ip: "${{{host_var}}}"', compose_sources)

    def test_runtime_env_allows_tagged_third_party_images_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stack_dir = Path(temp_dir) / "xian-stack"
            stack_dir.mkdir()
            with patch.object(backend, "STACK_DIR", stack_dir):
                with patch.dict(os.environ, {}, clear=True):
                    env = backend.runtime_env(**runtime_env_kwargs(bds_enabled=True))

        self.assertEqual("postgres:17.10", env["XIAN_POSTGRES_IMAGE"])
        self.assertEqual("prom/prometheus:v3.12.0", env["XIAN_PROMETHEUS_IMAGE"])
        self.assertEqual("grafana/grafana:12.4.5", env["XIAN_GRAFANA_IMAGE"])

    def test_runtime_env_rejects_tagged_third_party_images_when_strict(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stack_dir = Path(temp_dir) / "xian-stack"
            stack_dir.mkdir()
            with patch.object(backend, "STACK_DIR", stack_dir):
                with patch.dict(
                    os.environ,
                    {"XIAN_REQUIRE_DIGEST_PINNED_THIRD_PARTY_IMAGES": "1"},
                    clear=True,
                ):
                    with self.assertRaisesRegex(
                        ValueError,
                        "XIAN_POSTGRES_IMAGE must be digest-pinned",
                    ):
                        backend.runtime_env(**runtime_env_kwargs(bds_enabled=True))

    def test_runtime_env_allows_digest_pinned_third_party_images_when_strict(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stack_dir = Path(temp_dir) / "xian-stack"
            stack_dir.mkdir()
            with patch.object(backend, "STACK_DIR", stack_dir):
                with patch.dict(
                    os.environ,
                    {
                        "XIAN_REQUIRE_DIGEST_PINNED_THIRD_PARTY_IMAGES": "1",
                        "XIAN_POSTGRES_IMAGE": PINNED_POSTGRES_IMAGE,
                        "XIAN_PROMETHEUS_IMAGE": PINNED_PROMETHEUS_IMAGE,
                        "XIAN_GRAFANA_IMAGE": PINNED_GRAFANA_IMAGE,
                    },
                    clear=True,
                ):
                    env = backend.runtime_env(**runtime_env_kwargs(bds_enabled=True))

        self.assertEqual(PINNED_POSTGRES_IMAGE, env["XIAN_POSTGRES_IMAGE"])
        self.assertEqual(PINNED_PROMETHEUS_IMAGE, env["XIAN_PROMETHEUS_IMAGE"])
        self.assertEqual(PINNED_GRAFANA_IMAGE, env["XIAN_GRAFANA_IMAGE"])


if __name__ == "__main__":
    unittest.main()
