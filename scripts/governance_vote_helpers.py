"""Helpers for proposal flows that finalize as soon as a threshold is reached."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

StatusFetcher = Callable[[], Awaitable[dict[str, Any]]]
VoteSender = Callable[[], Awaitable[dict[str, Any]]]


def vote_status_progress(status: dict[str, Any]) -> tuple[int, int, int]:
    yes_votes = int(status.get("yes_votes", status.get("yes", 0)) or 0)
    no_votes = int(status.get("no_votes", status.get("no", 0)) or 0)
    yes_weight = int(status.get("yes_weight", 0) or 0)
    no_weight = int(status.get("no_weight", 0) or 0)
    voters = status.get("voters") or []
    return yes_votes + no_votes, yes_weight + no_weight, len(voters)


async def read_freshest_status(
    fetch_statuses: Sequence[StatusFetcher],
    *,
    completed_statuses: set[str] | frozenset[str],
    label: str,
) -> dict[str, Any]:
    if not fetch_statuses:
        raise ValueError("at least one status reader is required")

    results = await asyncio.gather(
        *(fetch_status() for fetch_status in fetch_statuses),
        return_exceptions=True,
    )
    statuses: list[dict[str, Any]] = []
    errors: list[str] = []
    for result in results:
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, BaseException):
            errors.append(f"{type(result).__name__}: {result}")
            continue
        if not isinstance(result, dict) or not isinstance(result.get("status"), str):
            errors.append(f"invalid status payload: {result!r}")
            continue
        statuses.append(result)

    if not statuses:
        raise RuntimeError(f"{label} could not be read; errors={errors}")

    completed = [
        status for status in statuses if status.get("status") in completed_statuses
    ]
    if completed:
        return max(completed, key=vote_status_progress)
    return max(statuses, key=vote_status_progress)


async def wait_for_status(
    fetch_status: StatusFetcher,
    *,
    expected_status: str,
    label: str,
    timeout_seconds: float = 15.0,
    poll_interval_seconds: float = 0.5,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_status = None
    while time.monotonic() < deadline:
        status = await fetch_status()
        last_status = status
        if status["status"] == expected_status:
            return status
        await asyncio.sleep(poll_interval_seconds)
    raise RuntimeError(f"{label} did not reach {expected_status!r}; last={last_status}")


async def cast_votes_until_status(
    vote_senders: Sequence[VoteSender],
    *,
    fetch_status: StatusFetcher,
    completed_statuses: set[str] | frozenset[str],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    current_status = await fetch_status()
    if current_status["status"] in completed_statuses:
        return [], current_status

    vote_receipts: list[dict[str, Any]] = []
    for send_vote in vote_senders:
        current_status = await fetch_status()
        if current_status["status"] in completed_statuses:
            return vote_receipts, current_status
        try:
            vote_receipt = await send_vote()
        except Exception:
            current_status = await fetch_status()
            if current_status["status"] in completed_statuses:
                return vote_receipts, current_status
            raise
        vote_receipts.append(vote_receipt)
        current_status = await fetch_status()
        if current_status["status"] in completed_statuses:
            return vote_receipts, current_status

    return vote_receipts, None
