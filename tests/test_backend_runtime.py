from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import backend


class BackendRuntimeTests(unittest.TestCase):
    def test_run_make_target_streams_output_to_stderr(self) -> None:
        completed = subprocess.CompletedProcess(["make", "node-start-bds"], 0)

        with patch("backend.subprocess.run", return_value=completed) as run_mock:
            result = backend.run_make_target(
                "node-start-bds",
                stream_output=True,
                env={"XIAN_BDS_ENABLED": "1"},
            )

        self.assertEqual(result, completed)
        args, kwargs = run_mock.call_args
        self.assertEqual(args[0], ["make", "node-start-bds"])
        self.assertIs(kwargs["stdout"], sys.stderr)
        self.assertIs(kwargs["stderr"], sys.stderr)
        self.assertNotIn("capture_output", kwargs)
        self.assertEqual(kwargs["env"], {"XIAN_BDS_ENABLED": "1"})


if __name__ == "__main__":
    unittest.main()
