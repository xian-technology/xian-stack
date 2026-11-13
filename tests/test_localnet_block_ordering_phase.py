from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "localnet-e2e.py"
sys.path.insert(0, str(MODULE_PATH.parent))

if "localnet_e2e" in sys.modules:
    localnet_e2e = sys.modules["localnet_e2e"]
else:
    SPEC = importlib.util.spec_from_file_location("localnet_e2e", MODULE_PATH)
    if SPEC is None or SPEC.loader is None:
        raise RuntimeError(f"unable to load {MODULE_PATH}")
    localnet_e2e = importlib.util.module_from_spec(SPEC)
    sys.modules[SPEC.name] = localnet_e2e
    SPEC.loader.exec_module(localnet_e2e)


class LocalnetBlockOrderingPhaseTests(unittest.TestCase):
    def test_block_ordering_phase_is_part_of_five_node_e2e_sequence(self) -> None:
        phase_names = localnet_e2e.E2ERunner.phase_names()
        index = phase_names.index("16-block-ordering")

        self.assertEqual("15-shielded-note-token", phase_names[index - 1])
        self.assertEqual("17-chaos-convergence", phase_names[index + 1])
        self.assertTrue(hasattr(localnet_e2e.E2ERunner, "block_ordering_phase"))

    def test_e2e_registry_has_no_parallel_executor_phases(self) -> None:
        phase_names = localnet_e2e.E2ERunner.phase_names()

        self.assertNotIn("16-parallel-execution", phase_names)
        self.assertNotIn("22-mixed-execution", phase_names)


if __name__ == "__main__":
    unittest.main()
