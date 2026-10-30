from __future__ import annotations

import tomllib
import unittest
from pathlib import Path


class PythonRuntimeRequirementsTests(unittest.TestCase):
    def test_protobuf_pin_matches_node_runtime_requirement(self) -> None:
        stack = Path(__file__).resolve().parents[1]
        metadata = tomllib.loads((stack.parent / "xian-abci" / "pyproject.toml").read_text())
        expected = next(
            requirement
            for requirement in metadata["project"]["dependencies"]
            if requirement.startswith("protobuf==")
        )
        runtime = (stack / "docker" / "python-runtime-requirements.txt").read_text().splitlines()
        actual = next(line.split()[0] for line in runtime if line.startswith("protobuf=="))
        self.assertEqual(actual, expected, "Node images must match generated Protobuf requirements")

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
