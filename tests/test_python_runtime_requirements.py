from __future__ import annotations

import unittest
from pathlib import Path


class PythonRuntimeRequirementsTests(unittest.TestCase):
    def test_runtime_requirements_are_hash_pinned_and_no_editables(self) -> None:
        path = Path(__file__).resolve().parents[1] / "docker" / "python-runtime-requirements.txt"
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertTrue(lines)
        self.assertFalse(any(line.startswith("-e ") for line in lines))
        self.assertTrue(
            any("--hash=sha256:" in line for line in lines),
            "runtime requirements must stay hash-pinned",
        )


if __name__ == "__main__":
    unittest.main()
