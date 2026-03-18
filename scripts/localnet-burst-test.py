#!/usr/bin/env python3
"""Compatibility wrapper for the generic localnet workload runner."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
WORKLOAD_SCRIPT = SCRIPT_DIR / "localnet-workload.py"


def main(argv: list[str] | None = None) -> int:
    forwarded = list(argv if argv is not None else sys.argv[1:])
    cmd = [
        sys.executable,
        str(WORKLOAD_SCRIPT),
        "--scenario",
        "counter_basic",
        *forwarded,
    ]
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
