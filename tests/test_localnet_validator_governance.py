from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "localnet-validator-governance.py"
sys.path.insert(0, str(MODULE_PATH.parent))

if "localnet_validator_governance" in sys.modules:
    localnet_validator_governance = sys.modules["localnet_validator_governance"]
else:
    SPEC = importlib.util.spec_from_file_location(
        "localnet_validator_governance",
        MODULE_PATH,
    )
    if SPEC is None or SPEC.loader is None:
        raise RuntimeError(f"unable to load {MODULE_PATH}")
    localnet_validator_governance = importlib.util.module_from_spec(SPEC)
    sys.modules[SPEC.name] = localnet_validator_governance
    SPEC.loader.exec_module(localnet_validator_governance)


class LocalnetValidatorGovernanceTests(unittest.TestCase):
    def test_client_config_uses_localnet_rpc_timeout(self) -> None:
        args = localnet_validator_governance.build_parser().parse_args(
            [
                "--rpc-timeout-seconds",
                "123",
            ]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(
                localnet_validator_governance,
                "OUTPUT_ROOT",
                Path(tmpdir),
            ):
                runner = localnet_validator_governance.ValidatorGovernanceRunner(args)

        config = runner.client_config()

        self.assertEqual(123, config.submission.timeout_seconds)
        self.assertEqual(0.5, config.submission.poll_interval_seconds)
        self.assertEqual(6, config.retry.max_attempts)


if __name__ == "__main__":
    unittest.main()
