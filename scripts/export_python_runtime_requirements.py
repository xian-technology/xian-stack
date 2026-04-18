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


def strip_local_editables(exported: str) -> str:
    filtered: list[str] = []
    skip_editable_comments = False
    for line in exported.splitlines():
        if line.startswith("-e "):
            skip_editable_comments = True
            continue
        if skip_editable_comments:
            if not line.strip() or line.startswith("    #"):
                continue
            skip_editable_comments = False
        filtered.append(line)
    return "\n".join(filtered).strip() + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Export a lock-backed Python runtime requirements file for "
            "xian-stack node images."
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
        default=Path(__file__).resolve().parents[1]
        / "docker"
        / "python-runtime-requirements.txt",
        help="Path to the generated requirements file.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    workspace_root = args.workspace_root.resolve()
    exported = export_requirements(workspace_root / "xian-abci")
    filtered = strip_local_editables(exported)
    args.output.write_text(filtered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
