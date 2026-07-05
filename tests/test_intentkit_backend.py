from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import intentkit_backend


class IntentKitBackendTests(unittest.TestCase):
    def test_endpoints_bracket_ipv6_hosts(self) -> None:
        endpoints = intentkit_backend.intentkit_endpoints(
            bind_host="::1",
            frontend_port=39000,
            api_port=39080,
            s3_port=39001,
        )

        self.assertEqual("http://[::1]:39000", endpoints["intentkit"])
        self.assertEqual("http://[::1]:39080", endpoints["intentkit_api"])
        self.assertEqual("http://[::1]:39001/static", endpoints["intentkit_static"])

    def test_intentkit_rpc_url_brackets_non_loopback_ipv6_hosts(self) -> None:
        self.assertEqual(
            "http://[2001:db8::1]:26657",
            intentkit_backend._intentkit_rpc_url("http://[2001:db8::1]:26657/status"),
        )

    def test_intentkit_rpc_url_maps_ipv6_loopback_to_stack_service(self) -> None:
        self.assertEqual(
            "http://abci:26657",
            intentkit_backend._intentkit_rpc_url("http://[::1]:26657/status"),
        )

    def test_intentkit_rpc_url_maps_wildcard_to_stack_service(self) -> None:
        self.assertEqual(
            "http://abci:26657",
            intentkit_backend._intentkit_rpc_url("http://0.0.0.0:26657/status"),
        )

    def test_ensure_env_uses_stack_s3_port_for_static_url(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo_dir = root / "xian-intentkit"
            deployment_dir = repo_dir / "deployment"
            deployment_dir.mkdir(parents=True)
            (deployment_dir / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
            (repo_dir / ".env.example").write_text(
                "\n".join(
                    [
                        "APP_BASE_URL=http://localhost:3000",
                        "AWS_S3_CDN_URL=http://localhost:9000/static",
                        "XIAN_AGENT_LOGO_URL=http://localhost:8000/xian.jpg",
                        "XIAN_LOCALNET_RPC_URL=http://localhost:26657",
                        "XIAN_LOCALNET_CHAIN_ID=xian-local",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            env_file = root / "intentkit.env"

            result = intentkit_backend.ensure_intentkit_env(
                network_id="xian-localnet",
                chain_id="xian-local-test-1",
                rpc_status_url="http://127.0.0.1:26657/status",
                bind_host="127.0.0.1",
                frontend_port=39000,
                api_port=39080,
                env={
                    "XIAN_INTENTKIT_DIR": str(repo_dir),
                    "XIAN_INTENTKIT_ENV_FILE": str(env_file),
                    "XIAN_INTENTKIT_S3_PORT": "39001",
                },
            )

            content = env_file.read_text(encoding="utf-8")
            self.assertIn("APP_BASE_URL=http://127.0.0.1:39000", content)
            self.assertIn("AWS_S3_CDN_URL=http://127.0.0.1:39001/static", content)
            self.assertIn("XIAN_LOCALNET_RPC_URL=http://abci:26657", content)
            self.assertEqual("http://127.0.0.1:39001/static", result["intentkit_static"])

    def test_ensure_env_does_not_preserve_stale_derived_only_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo_dir = root / "xian-intentkit"
            deployment_dir = repo_dir / "deployment"
            deployment_dir.mkdir(parents=True)
            (deployment_dir / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
            (repo_dir / ".env.example").write_text(
                "CUSTOM_KEEP=from-template\n",
                encoding="utf-8",
            )
            env_file = root / "intentkit.env"
            env_file.write_text(
                "\n".join(
                    [
                        "CUSTOM_KEEP=from-current",
                        "XIAN_AGENT_LOGO_URL=http://127.0.0.1:38080/skills/xian/xian.jpg",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            intentkit_backend.ensure_intentkit_env(
                network_id="xian-localnet",
                chain_id="xian-local-test-1",
                rpc_status_url="http://[::1]:26657/status",
                bind_host="::1",
                frontend_port=39000,
                api_port=39080,
                env={
                    "XIAN_INTENTKIT_DIR": str(repo_dir),
                    "XIAN_INTENTKIT_ENV_FILE": str(env_file),
                    "XIAN_INTENTKIT_S3_PORT": "39001",
                },
            )

            content = env_file.read_text(encoding="utf-8")
            self.assertIn("CUSTOM_KEEP=from-current", content)
            self.assertNotIn("http://127.0.0.1:38080/skills/xian/xian.jpg", content)
            self.assertEqual(content.count("XIAN_AGENT_LOGO_URL="), 1)
            self.assertIn('XIAN_AGENT_LOGO_URL="http://[::1]:39080/skills/xian/xian.jpg"', content)


if __name__ == "__main__":
    unittest.main()
