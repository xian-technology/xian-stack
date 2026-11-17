from __future__ import annotations

import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

MODULE_PATH = Path(__file__).absolute().parents[1] / "scripts" / "localnet-e2e.py"
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
    def test_full_suite_measures_idle_cadence_before_contract_workloads(self):
        names = localnet_e2e.E2ERunner.phase_names()
        self.assertLess(names.index("02-xian-py-smoke"), names.index("02-block-cadence"))
        self.assertLess(names.index("02-block-cadence"), names.index("03-contract-orchestration"))
        import localnet_cadence_checks
        import localnet_idle_unbond_checks

        runner, session = object(), object()
        with (
            patch.object(
                localnet_cadence_checks, "cadence_rehearsal", AsyncMock(return_value={"cases": [1]})
            ) as check,
            patch.object(
                localnet_idle_unbond_checks,
                "positive_unbond_rehearsal",
                AsyncMock(return_value={"anchored": True}),
            ) as unbond,
        ):
            result = asyncio.run(localnet_e2e.E2ERunner.block_cadence_phase(runner, session))
        self.assertEqual(result, {"cadence": {"cases": [1]}, "positive_unbond": {"anchored": True}})
        check.assert_awaited_once_with(
            runner, session, label="saturated-retention", expected_pending=False
        )
        unbond.assert_awaited_once_with(runner, session)

    def test_full_suite_selects_periodic_blocks_even_with_throughput_tuning(self):
        args = localnet_e2e.build_parser().parse_args([])
        with (
            patch.dict("os.environ", {"XIAN_LOCALNET_PROFILE": "throughput"}, clear=True),
            patch.object(localnet_e2e, "load_stack_env", return_value={}),
        ):
            env = localnet_e2e.make_localnet_env(args)
        self.assertEqual(env["XIAN_LOCALNET_BLOCK_POLICY_MODE"], "periodic")
        self.assertEqual(env["XIAN_LOCALNET_BLOCK_POLICY_INTERVAL"], "1s")

    def test_full_suite_refuses_an_explicit_incompatible_block_policy(self):
        args = localnet_e2e.build_parser().parse_args([])
        with (
            patch.dict("os.environ", {"XIAN_LOCALNET_BLOCK_POLICY_MODE": "on_demand"}, clear=True),
            patch.object(localnet_e2e, "load_stack_env", return_value={}),
        ):
            with self.assertRaisesRegex(localnet_e2e.E2EError, "requires periodic"):
                localnet_e2e.make_localnet_env(args)

    def test_full_suite_uses_defaults_for_make_exported_empty_policy_values(self):
        args = localnet_e2e.build_parser().parse_args([])
        for mode, interval, expected_interval in (
            ("", "", "1s"),
            ("periodic", "", "1s"),
            ("", "2s", "2s"),
        ):
            with (
                self.subTest(mode=mode, interval=interval),
                patch.dict(
                    "os.environ",
                    {
                        "XIAN_LOCALNET_BLOCK_POLICY_MODE": mode,
                        "XIAN_LOCALNET_BLOCK_POLICY_INTERVAL": interval,
                    },
                    clear=True,
                ),
                patch.object(localnet_e2e, "load_stack_env", return_value={}),
            ):
                env = localnet_e2e.make_localnet_env(args)
                self.assertEqual(env["XIAN_LOCALNET_BLOCK_POLICY_MODE"], "periodic")
                self.assertEqual(env["XIAN_LOCALNET_BLOCK_POLICY_INTERVAL"], expected_interval)

    def test_full_suite_preserves_an_explicit_periodic_interval(self):
        args = localnet_e2e.build_parser().parse_args([])
        with (
            patch.dict(
                "os.environ",
                {
                    "XIAN_LOCALNET_BLOCK_POLICY_MODE": "periodic",
                    "XIAN_LOCALNET_BLOCK_POLICY_INTERVAL": "2s",
                },
                clear=True,
            ),
            patch.object(localnet_e2e, "load_stack_env", return_value={}),
        ):
            self.assertEqual(
                localnet_e2e.make_localnet_env(args)["XIAN_LOCALNET_BLOCK_POLICY_INTERVAL"], "2s"
            )

    def test_block_ordering_phase_is_part_of_five_node_e2e_sequence(self) -> None:
        phase_names = localnet_e2e.E2ERunner.phase_names()
        index = phase_names.index("16-block-ordering")

        self.assertEqual("15-zk-feature-toggle", phase_names[index - 1])
        self.assertEqual("17-chaos-convergence", phase_names[index + 1])
        self.assertTrue(hasattr(localnet_e2e.E2ERunner, "block_ordering_phase"))

    def test_e2e_registry_has_no_parallel_executor_phases(self) -> None:
        phase_names = localnet_e2e.E2ERunner.phase_names()

        self.assertNotIn("16-parallel-execution", phase_names)
        self.assertNotIn("22-mixed-execution", phase_names)


if __name__ == "__main__":
    unittest.main()
