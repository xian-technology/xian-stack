from __future__ import annotations

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

        self.assertIn("bds_flag='--bds-enabled'", makefile)
        self.assertIn("bds_catchup_flag='--bds-catchup-enabled'", makefile)

    def test_stack_env_exports_bds_runtime_options(self) -> None:
        stack_env = (STACK_ROOT / "scripts" / "stack-env.sh").read_text(
            encoding="utf-8"
        )

        for env_key in BDS_RUNTIME_ENV_KEYS:
            with self.subTest(env_key=env_key):
                self.assertIn(f"export {env_key}=", stack_env)

    def test_compose_overlay_passes_bds_runtime_environment(self) -> None:
        compose = (STACK_ROOT / "docker-compose-abci-bds.yml").read_text(
            encoding="utf-8"
        )

        for env_key in BDS_RUNTIME_ENV_KEYS:
            with self.subTest(env_key=env_key):
                self.assertIn(f"{env_key}: ${{{env_key}}}", compose)


if __name__ == "__main__":
    unittest.main()
