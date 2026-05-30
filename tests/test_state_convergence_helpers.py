from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from state_convergence_helpers import wait_for_uniform_state


class StateConvergenceHelpersTests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_for_uniform_state_returns_expected_value_after_retries(self) -> None:
        responses = iter(
            [
                {"node-0": 321, "node-1": 198},
                {"node-0": 198, "node-1": 198},
            ]
        )

        async def fetch_values() -> dict[str, int]:
            return next(responses)

        observed = await wait_for_uniform_state(
            fetch_values=fetch_values,
            label="direct remaining allowance",
            expected=198,
            timeout_seconds=0.1,
            poll_interval_seconds=0.0,
        )

        self.assertEqual({"node-0": "198", "node-1": "198"}, observed)

    async def test_wait_for_uniform_state_accepts_uniform_value_without_expected_target(
        self,
    ) -> None:
        async def fetch_values() -> dict[str, str]:
            return {"node-0": "ready", "node-1": "ready"}

        observed = await wait_for_uniform_state(
            fetch_values=fetch_values,
            label="cluster health",
            timeout_seconds=0.1,
            poll_interval_seconds=0.0,
        )

        self.assertEqual({"node-0": "ready", "node-1": "ready"}, observed)

    async def test_wait_for_uniform_state_times_out_with_last_values_and_heights(self) -> None:
        async def fetch_values() -> dict[str, int]:
            return {"node-0": 198, "node-1": 321}

        async def fetch_heights() -> dict[str, int]:
            return {"node-0": 17, "node-1": 16}

        with self.assertRaisesRegex(
            RuntimeError,
            r"direct remaining allowance did not converge before timeout; "
            r"last_values=\{'node-0': '198', 'node-1': '321'\}; "
            r"expected='198'; "
            r"last_heights=\{'node-0': 17, 'node-1': 16\}",
        ):
            await wait_for_uniform_state(
                fetch_values=fetch_values,
                fetch_heights=fetch_heights,
                label="direct remaining allowance",
                expected=198,
                timeout_seconds=0.01,
                poll_interval_seconds=0.0,
            )


if __name__ == "__main__":
    unittest.main()
