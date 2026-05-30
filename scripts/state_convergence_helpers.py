from __future__ import annotations

import asyncio
import time
from typing import Any

_UNSET = object()


async def wait_for_uniform_state(
    *,
    fetch_values,
    label: str,
    normalize_value=lambda value: value,
    expected: Any = _UNSET,
    fetch_heights=None,
    timeout_seconds: float = 10.0,
    poll_interval_seconds: float = 0.25,
) -> dict[str, str]:
    deadline = time.monotonic() + timeout_seconds
    last_values: dict[str, str] | None = None
    last_heights: dict[str, int] | None = None
    expected_value = None if expected is _UNSET else str(normalize_value(expected))

    while time.monotonic() < deadline:
        values = await fetch_values()
        normalized = {node: str(normalize_value(value)) for node, value in values.items()}
        last_values = normalized
        uniform = len(set(normalized.values())) == 1
        if uniform:
            observed = next(iter(normalized.values()))
            if expected is _UNSET or observed == expected_value:
                return normalized
        if fetch_heights is not None:
            last_heights = await fetch_heights()
        await asyncio.sleep(poll_interval_seconds)

    details = [f"{label} did not converge before timeout", f"last_values={last_values!r}"]
    if expected is not _UNSET:
        details.append(f"expected={expected_value!r}")
    if last_heights is not None:
        details.append(f"last_heights={last_heights!r}")
    raise RuntimeError("; ".join(details))
