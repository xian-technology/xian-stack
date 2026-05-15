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


class LocalnetParallelPhaseTests(unittest.TestCase):
    def test_parallel_phase_is_part_of_five_node_e2e_sequence(self) -> None:
        phase_names = localnet_e2e.E2ERunner.phase_names()
        parallel_index = phase_names.index("16-parallel-execution")

        self.assertEqual("15-shielded-note-token", phase_names[parallel_index - 1])
        self.assertEqual("17-chaos-convergence", phase_names[parallel_index + 1])

    def test_known_transfer_metadata_requires_estimated_speculation(self) -> None:
        metadata = {
            "parallel_enabled": True,
            "parallel_estimated_known_transactions": 16,
            "parallel_estimated_unknown_transactions": 0,
            "parallel_estimated_parallelizable_transactions": 15,
            "parallel_planned_parallelizable_transactions": 15,
            "parallel_speculative_wave_count": 1,
            "parallel_speculative_accepted": 16,
        }

        self.assertTrue(localnet_e2e.parallel_metadata_has_known_speculation(metadata))

        metadata["parallel_estimated_unknown_transactions"] = 1
        self.assertFalse(localnet_e2e.parallel_metadata_has_known_speculation(metadata))

    def test_unknown_custom_contract_metadata_requires_serial_prefilter(self) -> None:
        metadata = {
            "parallel_enabled": True,
            "parallel_estimated_unknown_transactions": 10,
            "parallel_serial_prefiltered": 10,
            "parallel_speculative_wave_count": 0,
            "parallel_speculative_accepted": 0,
        }

        self.assertTrue(localnet_e2e.parallel_metadata_has_unknown_prefilter(metadata))

        metadata["parallel_speculative_accepted"] = 1
        self.assertFalse(localnet_e2e.parallel_metadata_has_unknown_prefilter(metadata))

    def test_custom_probe_expectations_follow_access_estimate_mode(self) -> None:
        estimated_unknown_metadata = {
            "parallel_enabled": True,
            "parallel_estimated_unknown_transactions": 12,
            "parallel_serial_prefiltered": 12,
            "parallel_speculative_wave_count": 0,
            "parallel_speculative_accepted": 0,
        }
        legacy_speculative_metadata = {
            "parallel_enabled": True,
            "parallel_speculative_accepted": 12,
            "parallel_planned_parallelizable_transactions": 11,
            "parallel_speculative_wave_count": 2,
            "parallel_serial_prefiltered": 1,
        }

        estimated_expectations = dict(
            localnet_e2e.parallel_custom_probe_batch_expectations(
                access_estimates_enabled=True,
            )
        )
        legacy_expectations = dict(
            localnet_e2e.parallel_custom_probe_batch_expectations(
                access_estimates_enabled=False,
            )
        )

        self.assertEqual(
            {
                "non_conflicting",
                "same_sender",
                "read_after_write",
                "prefix_scan",
            },
            set(estimated_expectations),
        )
        for predicate in estimated_expectations.values():
            self.assertTrue(predicate(estimated_unknown_metadata))
            self.assertFalse(predicate(legacy_speculative_metadata))

        self.assertTrue(
            legacy_expectations["non_conflicting"](legacy_speculative_metadata)
        )
        self.assertTrue(legacy_expectations["same_sender"](legacy_speculative_metadata))
        self.assertTrue(
            legacy_expectations["read_after_write"](legacy_speculative_metadata)
        )
        self.assertTrue(legacy_expectations["prefix_scan"](legacy_speculative_metadata))


if __name__ == "__main__":
    unittest.main()
