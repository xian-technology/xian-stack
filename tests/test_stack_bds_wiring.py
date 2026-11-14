from __future__ import annotations

import re
import unittest
from pathlib import Path

STACK_ROOT = Path(__file__).absolute().parents[1]

# BDS node settings live only in the node's xian.toml [bds] table; these
# variables were a second, silently merged source and are gone everywhere.
REMOVED_BDS_SETTING_VARIABLES = tuple(
    f"XIAN_BDS_{name}"
    for name in (
        "DSN",
        "HOST",
        "PORT",
        "DATABASE",
        "USER",
        "PASSWORD",
        "POOL_MIN_SIZE",
        "POOL_MAX_SIZE",
        "STATEMENT_TIMEOUT_MS",
        "ACQUIRE_TIMEOUT_MS",
        "APPLICATION_NAME",
        "QUEUE_MAX_SIZE",
        "CATCHUP_ENABLED",
        "CATCHUP_POLL_SECONDS",
        "RPC_URL",
        "SPOOL_DIR",
        "SPOOL_WARN_ENTRIES",
        "SPOOL_WARN_BYTES",
        "DISK_FREE_WARN_BYTES",
    )
)
SECRET = "/run/secrets/xian_bds_password"
CONFIGURATION_SURFACES = (
    "Makefile",
    "docker-compose-abci.yml",
    "docker-compose-abci-bds.yml",
    "docker-compose-abci-dev.yml",
    "scripts/stack-env.sh",
    "scripts/configure_node.py",
    "scripts/backend.py",
    "scripts/localnet-init.py",
    "scripts/localnet-e2e.py",
    "scripts/storage-report.py",
    "docker/postgres/init-postgraphile-role.sh",
)


def service_block(compose: str, name: str) -> str:
    start = compose.index(f"\n  {name}:\n")
    following = re.search(r"\n  [a-z][a-z0-9-]*:\n", compose[start + 1 :])
    return compose[start : start + 1 + following.start()] if following else compose[start:]


POSTGRAPHILE_HARDENING_ENV_KEYS = {
    "XIAN_POSTGRAPHILE_STATEMENT_TIMEOUT_MS",
    "XIAN_POSTGRAPHILE_BODY_SIZE_LIMIT_BYTES",
    "XIAN_POSTGRAPHILE_DISABLE_DEFAULT_MUTATIONS",
    "XIAN_POSTGRAPHILE_SIMPLE_COLLECTIONS",
    "XIAN_POSTGRAPHILE_SCHEMA_WAIT_TIMEOUT_SECONDS",
    "XIAN_POSTGRAPHILE_REQUIRED_TABLES",
}


class StackBdsWiringTests(unittest.TestCase):
    def test_no_configuration_surface_carries_removed_bds_settings(self) -> None:
        for relative in CONFIGURATION_SURFACES:
            text = (STACK_ROOT / relative).read_text(encoding="utf-8")
            for name in REMOVED_BDS_SETTING_VARIABLES:
                with self.subTest(file=relative, name=name):
                    self.assertIsNone(re.search(rf"\b{name}\b", text))

    def test_node_and_postgres_read_one_mounted_password_secret(self) -> None:
        compose = (STACK_ROOT / "docker-compose-abci-bds.yml").read_text(encoding="utf-8")
        abci = service_block(compose, "abci")
        postgres = service_block(compose, "postgres")
        init = service_block(compose, "postgraphile-db-init")

        self.assertIn(
            "secrets:\n  xian_bds_password:\n    file: ${XIAN_BDS_PASSWORD_FILE:?", compose
        )
        self.assertNotIn("environment:", abci)
        for block in (abci, postgres, init):
            self.assertIn("    secrets:\n      - xian_bds_password\n", block)
        self.assertIn(f"POSTGRES_PASSWORD_FILE: {SECRET}", postgres)
        self.assertNotIn("POSTGRES_PASSWORD:", postgres)
        self.assertIn(f"XIAN_BDS_PASSWORD_FILE: {SECRET}", init)
        self.assertNotIn("PGPASSWORD", init)

    def test_postgraphile_role_init_reads_the_mounted_bds_secret(self) -> None:
        role_init = (STACK_ROOT / "docker" / "postgres" / "init-postgraphile-role.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn('PGPASSWORD="$(cat "${XIAN_BDS_PASSWORD_FILE:?', role_init)

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
