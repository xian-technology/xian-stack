from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

STACK_ROOT = Path(__file__).resolve().parents[1]

BDS_RUNTIME_ENV_KEYS = {
    "XIAN_BDS_DSN": "--bds-dsn",
    "XIAN_BDS_HOST": "--bds-host",
    "XIAN_BDS_PORT": "--bds-port",
    "XIAN_BDS_DATABASE": "--bds-database",
    "XIAN_BDS_USER": "--bds-user",
    "XIAN_BDS_PASSWORD": "--bds-password",
    "XIAN_BDS_POOL_MIN_SIZE": "--bds-pool-min-size",
    "XIAN_BDS_POOL_MAX_SIZE": "--bds-pool-max-size",
    "XIAN_BDS_STATEMENT_TIMEOUT_MS": "--bds-statement-timeout-ms",
    "XIAN_BDS_ACQUIRE_TIMEOUT_MS": "--bds-acquire-timeout-ms",
    "XIAN_BDS_APPLICATION_NAME": "--bds-application-name",
    "XIAN_BDS_QUEUE_MAX_SIZE": "--bds-queue-max-size",
    "XIAN_BDS_CATCHUP_ENABLED": "--bds-catchup-enabled",
    "XIAN_BDS_CATCHUP_POLL_SECONDS": "--bds-catchup-poll-seconds",
    "XIAN_BDS_RPC_URL": "--bds-rpc-url",
    "XIAN_BDS_SPOOL_DIR": "--bds-spool-dir",
    "XIAN_BDS_SPOOL_WARN_ENTRIES": "--bds-spool-warn-entries",
    "XIAN_BDS_SPOOL_WARN_BYTES": "--bds-spool-warn-bytes",
    "XIAN_BDS_DISK_FREE_WARN_BYTES": "--bds-disk-free-warn-bytes",
}

SENSITIVE_ENV_KEYS = {
    "XIAN_BDS_DSN",
    "XIAN_BDS_PASSWORD",
}

POSTGRAPHILE_HARDENING_ENV_KEYS = {
    "XIAN_POSTGRAPHILE_STATEMENT_TIMEOUT_MS",
    "XIAN_POSTGRAPHILE_BODY_SIZE_LIMIT_BYTES",
    "XIAN_POSTGRAPHILE_DISABLE_DEFAULT_MUTATIONS",
    "XIAN_POSTGRAPHILE_SIMPLE_COLLECTIONS",
    "XIAN_POSTGRAPHILE_SCHEMA_WAIT_TIMEOUT_SECONDS",
    "XIAN_POSTGRAPHILE_REQUIRED_TABLES",
}


class StackBdsWiringTests(unittest.TestCase):
    def test_makefile_wires_bds_runtime_options(self) -> None:
        makefile = (STACK_ROOT / "Makefile").read_text(encoding="utf-8")

        for env_key, cli_flag in BDS_RUNTIME_ENV_KEYS.items():
            with self.subTest(env_key=env_key):
                self.assertIn(f"{env_key} ?", makefile)
                self.assertIn(f"export {env_key}", makefile)
                self.assertIn(cli_flag, makefile)
                if env_key not in SENSITIVE_ENV_KEYS:
                    self.assertIn(f'printf "{env_key}=', makefile)

        self.assertIn("XIAN_BDS_ENABLED_FLAG", makefile)
        self.assertIn("XIAN_APP_METRICS_ENABLED_FLAG", makefile)
        self.assertIn("XIAN_BDS_CATCHUP_ENABLED_FLAG", makefile)
        self.assertIn("XIAN_BLOCK_POLICY_MODE ?= periodic", makefile)
        self.assertIn("XIAN_BLOCK_POLICY_INTERVAL ?= 5s", makefile)
        self.assertIn("--block-policy-mode $(XIAN_BLOCK_POLICY_MODE)", makefile)
        self.assertIn("--block-policy-interval $(XIAN_BLOCK_POLICY_INTERVAL)", makefile)
        self.assertIn("XIAN_TX_FEE_MODE ?= paid_metered", makefile)
        self.assertIn("XIAN_FREE_TX_MAX_CHI ?= 1000000", makefile)
        self.assertIn("XIAN_FREE_BLOCK_MAX_CHI ?= 20000000", makefile)
        self.assertIn("--tx-fee-mode $(XIAN_TX_FEE_MODE)", makefile)
        self.assertIn("--free-tx-max-chi $(XIAN_FREE_TX_MAX_CHI)", makefile)
        self.assertIn("--free-block-max-chi $(XIAN_FREE_BLOCK_MAX_CHI)", makefile)

    def test_node_configure_renders_enabled_flags(self) -> None:
        rendered = subprocess.run(
            [
                "make",
                "-n",
                "node-configure",
                "XIAN_BDS_ENABLED=1",
                "XIAN_BDS_PASSWORD=test",
                "CONFIGURE_ARGS=--moniker test",
            ],
            cwd=STACK_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

        self.assertIn("xian-configure-node --bds-enabled --metrics-enabled", rendered)
        self.assertIn("--block-policy-mode periodic --block-policy-interval 5s", rendered)
        self.assertIn(
            "--tx-fee-mode paid_metered --free-tx-max-chi 1000000 --free-block-max-chi 20000000",
            rendered,
        )
        self.assertIn("--bds-catchup-enabled", rendered)
        self.assertNotIn("$bds_flag", rendered)
        self.assertNotIn("$metrics_flag", rendered)
        self.assertNotIn("$bds_catchup_flag", rendered)

    def test_node_configure_allows_block_policy_override(self) -> None:
        rendered = subprocess.run(
            [
                "make",
                "-n",
                "node-configure",
                "XIAN_BDS_PASSWORD=test",
                "XIAN_BLOCK_POLICY_MODE=on_demand",
                "XIAN_BLOCK_POLICY_INTERVAL=0s",
                "CONFIGURE_ARGS=--moniker test",
            ],
            cwd=STACK_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

        self.assertIn("--block-policy-mode on_demand --block-policy-interval 0s", rendered)

    def test_node_configure_allows_fee_policy_override(self) -> None:
        rendered = subprocess.run(
            [
                "make",
                "-n",
                "node-configure",
                "XIAN_BDS_PASSWORD=test",
                "XIAN_TX_FEE_MODE=free_metered",
                "XIAN_FREE_TX_MAX_CHI=250000",
                "XIAN_FREE_BLOCK_MAX_CHI=1000000",
                "CONFIGURE_ARGS=--moniker test",
            ],
            cwd=STACK_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

        self.assertIn(
            "--tx-fee-mode free_metered --free-tx-max-chi 250000 --free-block-max-chi 1000000",
            rendered,
        )

    def test_node_configure_renders_disabled_flags(self) -> None:
        rendered = subprocess.run(
            [
                "make",
                "-n",
                "node-configure",
                "XIAN_BDS_ENABLED=0",
                "XIAN_APP_METRICS_ENABLED=0",
                "XIAN_BDS_CATCHUP_ENABLED=0",
                "XIAN_BDS_PASSWORD=test",
                "CONFIGURE_ARGS=--moniker test",
            ],
            cwd=STACK_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

        self.assertIn("xian-configure-node --no-bds-enabled --no-metrics-enabled", rendered)
        self.assertIn("--no-bds-catchup-enabled", rendered)

    def test_stack_env_exports_bds_runtime_options(self) -> None:
        stack_env = (STACK_ROOT / "scripts" / "stack-env.sh").read_text(encoding="utf-8")

        for env_key in BDS_RUNTIME_ENV_KEYS:
            with self.subTest(env_key=env_key):
                self.assertIn(f"export {env_key}=", stack_env)
        self.assertIn("export XIAN_BLOCK_POLICY_MODE=", stack_env)
        self.assertIn("export XIAN_BLOCK_POLICY_INTERVAL=", stack_env)
        self.assertIn("export XIAN_TX_FEE_MODE=", stack_env)
        self.assertIn("export XIAN_FREE_TX_MAX_CHI=", stack_env)
        self.assertIn("export XIAN_FREE_BLOCK_MAX_CHI=", stack_env)

    def test_compose_overlay_passes_bds_runtime_environment(self) -> None:
        compose = (STACK_ROOT / "docker-compose-abci-bds.yml").read_text(encoding="utf-8")

        for env_key in BDS_RUNTIME_ENV_KEYS:
            with self.subTest(env_key=env_key):
                self.assertIn(f"{env_key}: ${{{env_key}}}", compose)

    def test_postgraphile_hardening_defaults_are_wired(self) -> None:
        makefile = (STACK_ROOT / "Makefile").read_text(encoding="utf-8")
        stack_env = (STACK_ROOT / "scripts" / "stack-env.sh").read_text(encoding="utf-8")
        compose = (STACK_ROOT / "docker-compose-abci-bds.yml").read_text(encoding="utf-8")
        role_init = (STACK_ROOT / "docker" / "postgres" / "init-postgraphile-role.sh").read_text(
            encoding="utf-8"
        )
        launcher = (STACK_ROOT / "docker" / "postgraphile" / "start-postgraphile.sh").read_text(
            encoding="utf-8"
        )
        graphile_config = (
            STACK_ROOT / "docker" / "postgraphile" / "graphile.config.mjs"
        ).read_text(encoding="utf-8")

        for env_key in POSTGRAPHILE_HARDENING_ENV_KEYS:
            with self.subTest(env_key=env_key):
                self.assertIn(f"{env_key} ?", makefile)
                self.assertIn(f"export {env_key}", makefile)
                self.assertIn(f'printf "{env_key}=', makefile)
                self.assertIn(f"export {env_key}=", stack_env)
                self.assertIn(f"${{{env_key}}}", compose)

        self.assertIn("ALTER ROLE %I SET statement_timeout", role_init)
        self.assertIn('-C "${POSTGRAPHILE_CONFIG', launcher)
        self.assertIn("wait-for-bds-schema.mjs", launcher)
        self.assertIn("disableDefaultMutations", graphile_config)
        self.assertIn("graphiql: true", graphile_config)
        self.assertIn("simpleCollections", graphile_config)
        self.assertIn("bodySizeLimit", graphile_config)

    def test_node_start_bds_uses_explicit_runtime_services(self) -> None:
        makefile = (STACK_ROOT / "Makefile").read_text(encoding="utf-8")

        self.assertIn(
            "$(ABCI_BDS_COMPOSE) up -d $(NODE_UP_BUILD_FLAG) abci postgres postgraphile",
            makefile,
        )


if __name__ == "__main__":
    unittest.main()
