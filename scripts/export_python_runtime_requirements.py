#!/usr/bin/env python3

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def _export(root: Path, *, extras: tuple[str, ...] = ()) -> str:
    command = [
        "uv",
        "export",
        "--frozen",
        "--no-dev",
        "--no-emit-local",
        "--format",
        "requirements-txt",
    ]
    for extra in extras:
        command.extend(["--extra", extra])
    result = subprocess.run(
        command,
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def merge_requirements(*exports: str) -> str:
    """Combine already hash-pinned locks, refusing conflicting shared pins.

    Both exports target the same Python runtime. Marker/spec changes also require
    lock alignment rather than silently selecting one dependency graph.
    """
    blocks: dict[str, list[str]] = {}
    for exported in exports:
        current: list[str] | None = None
        for line in exported.splitlines():
            if line and not line[0].isspace() and not line.startswith("#"):
                if "==" not in line:
                    raise ValueError("runtime requirement is not an exact version pin")
                name = line.split("==", 1)[0]
                if name in blocks:
                    if blocks[name][0] != line:
                        raise ValueError(
                            f"runtime lock conflict for {name}; align node and SDK locks"
                        )
                    current = None
                else:
                    current = [line]
                    blocks[name] = current
            elif current is not None:
                current.append(line)
    return (
        "# Generated from node and SDK hybrid runtime locks; do not edit by hand.\n"
        + "\n".join("\n".join(blocks[name]).rstrip() for name in sorted(blocks))
        + "\n"
    )


def export_requirements(abci_root: Path, *, sdk_root: Path | None = None) -> str:
    node = _export(abci_root)
    if sdk_root is None:
        return node
    return merge_requirements(node, _export(sdk_root, extras=("hybrid",)))


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
    exported = export_requirements(
        workspace_root / "xian-abci", sdk_root=workspace_root / "xian-py"
    )
    args.output.write_text(exported.strip() + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
