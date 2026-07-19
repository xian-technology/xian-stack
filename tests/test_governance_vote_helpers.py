from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from governance_vote_helpers import (
    cast_votes_until_status,
    read_freshest_status,
    wait_for_status,
)


class ProposalHarness:
    def __init__(self, *, votes_required: int):
        self.status = "pending"
        self.votes_cast = 0
        self.votes_required = votes_required

    async def fetch_status(self) -> dict[str, int | str]:
        return {
            "status": self.status,
            "votes_cast": self.votes_cast,
        }

    async def cast_vote(self) -> dict[str, int]:
        self.votes_cast += 1
        if self.votes_cast >= self.votes_required:
            self.status = "approved"
        return {"vote_number": self.votes_cast}


class GovernanceVoteHelpersTests(unittest.IsolatedAsyncioTestCase):
    async def test_freshest_status_prefers_terminal_snapshot(self) -> None:
        async def stale_reader() -> dict[str, int | str]:
            return {
                "status": "pending",
                "yes": 3,
                "yes_weight": 30,
            }

        async def fresh_reader() -> dict[str, int | str]:
            return {
                "status": "approved",
                "yes": 4,
                "yes_weight": 42,
            }

        status = await read_freshest_status(
            [stale_reader, fresh_reader],
            completed_statuses={"approved"},
            label="members vote 4",
        )

        self.assertEqual("approved", status["status"])
        self.assertEqual(42, status["yes_weight"])

    async def test_freshest_status_ignores_failed_reader(self) -> None:
        async def failed_reader() -> dict[str, int | str]:
            raise TimeoutError("lagging node")

        async def healthy_reader() -> dict[str, int | str]:
            return {"status": "pending", "yes_votes": 2, "yes_weight": 20}

        status = await read_freshest_status(
            [failed_reader, healthy_reader],
            completed_statuses={"executed"},
            label="governance proposal 7",
        )

        self.assertEqual(2, status["yes_votes"])

    async def test_cast_votes_stops_after_terminal_status(self) -> None:
        proposal = ProposalHarness(votes_required=2)

        vote_receipts, final_status = await cast_votes_until_status(
            [proposal.cast_vote, proposal.cast_vote, proposal.cast_vote],
            fetch_status=proposal.fetch_status,
            completed_statuses={"approved"},
        )

        self.assertEqual(2, len(vote_receipts))
        self.assertEqual(
            [{"vote_number": 1}, {"vote_number": 2}],
            vote_receipts,
        )
        self.assertIsNotNone(final_status)
        self.assertEqual("approved", final_status["status"])

    async def test_cast_votes_returns_none_when_voters_are_exhausted(self) -> None:
        proposal = ProposalHarness(votes_required=3)

        vote_receipts, final_status = await cast_votes_until_status(
            [proposal.cast_vote, proposal.cast_vote],
            fetch_status=proposal.fetch_status,
            completed_statuses={"approved"},
        )

        self.assertEqual(2, len(vote_receipts))
        self.assertIsNone(final_status)

    async def test_cast_votes_recovers_when_submission_races_with_finalization(
        self,
    ) -> None:
        proposal = ProposalHarness(votes_required=2)

        async def late_vote() -> dict[str, int]:
            proposal.status = "approved"
            raise RuntimeError("late vote rejected")

        vote_receipts, final_status = await cast_votes_until_status(
            [late_vote],
            fetch_status=proposal.fetch_status,
            completed_statuses={"approved"},
        )

        self.assertEqual([], vote_receipts)
        self.assertIsNotNone(final_status)
        self.assertEqual("approved", final_status["status"])

    async def test_cast_votes_preserves_nonterminal_submission_failure(self) -> None:
        proposal = ProposalHarness(votes_required=2)

        async def failed_vote() -> dict[str, int]:
            raise RuntimeError("transport failed")

        with self.assertRaisesRegex(RuntimeError, "transport failed"):
            await cast_votes_until_status(
                [failed_vote],
                fetch_status=proposal.fetch_status,
                completed_statuses={"approved"},
            )

    async def test_wait_for_status_polls_until_expected_status(self) -> None:
        proposal = ProposalHarness(votes_required=1)

        async def advance_and_fetch() -> dict[str, int | str]:
            if proposal.votes_cast == 0:
                await proposal.cast_vote()
            return await proposal.fetch_status()

        status = await wait_for_status(
            advance_and_fetch,
            expected_status="approved",
            label="proposal 7",
            timeout_seconds=0.05,
            poll_interval_seconds=0.0,
        )

        self.assertEqual("approved", status["status"])

    async def test_wait_for_status_times_out_with_context(self) -> None:
        proposal = ProposalHarness(votes_required=2)

        with self.assertRaisesRegex(
            RuntimeError,
            r"proposal 9 did not reach 'approved'; last=",
        ):
            await wait_for_status(
                proposal.fetch_status,
                expected_status="approved",
                label="proposal 9",
                timeout_seconds=0.01,
                poll_interval_seconds=0.0,
            )


if __name__ == "__main__":
    unittest.main()
