from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import intentkit_backend


class IntentKitBackendTests(unittest.TestCase):
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
            self.assertEqual("http://127.0.0.1:39001/static", result["intentkit_static"])


if __name__ == "__main__":
    unittest.main()
