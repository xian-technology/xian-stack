#!/usr/bin/env python3

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def export_requirements(abci_root: Path) -> str:
    command = [
        "uv",
        "export",
        "--frozen",
        "--no-dev",
        "--no-emit-local",
        "--format",
        "requirements-txt",
    ]
    result = subprocess.run(
        command,
        cwd=abci_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Export a lock-backed Python runtime requirements file for xian-stack node images."
        )
    )
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="Workspace root containing xian-abci and xian-stack.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "docker" / "python-runtime-requirements.txt",
        help="Path to the generated requirements file.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    workspace_root = args.workspace_root.resolve()
    exported = export_requirements(workspace_root / "xian-abci")
    args.output.write_text(exported.strip() + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
