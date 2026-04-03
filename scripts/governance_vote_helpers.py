"""Helpers for proposal flows that finalize as soon as a threshold is reached."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

StatusFetcher = Callable[[], Awaitable[dict[str, Any]]]
VoteSender = Callable[[], Awaitable[dict[str, Any]]]


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
    raise RuntimeError(
        f"{label} did not reach {expected_status!r}; last={last_status}"
    )


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
        vote_receipts.append(await send_vote())
        current_status = await fetch_status()
        if current_status["status"] in completed_statuses:
            return vote_receipts, current_status

    return vote_receipts, None
