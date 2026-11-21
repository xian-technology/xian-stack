"""Restore reviewed Rust wheel lockfiles into an immutable release checkout.

This only writes generated Cargo.lock files in a fresh build context. It never
updates a source ref or silently replaces a different local dependency lock.
"""

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

RUST_WHEELS = ("xian-compiler-core", "xian-vm-core", "xian-zk")
INPUTS_ROOT = Path(__file__).resolve().parents[1] / "release" / "rust-inputs"


def check_checkout(source: Path, ref: str) -> None:
    def git(*arguments):
        return subprocess.check_output(["git", "-C", str(source), *arguments], text=True).strip()

    if git("rev-parse", "HEAD") != ref:
        raise ValueError("contracting checkout differs from the release source")
    if git("status", "--porcelain", "--untracked-files=no"):
        raise ValueError("contracting checkout has modified tracked sources")


def lock_plan(source: Path, selected: Path, metadata: dict) -> list[tuple[Path, bytes]]:
    if set(metadata["sha256"]) != set(RUST_WHEELS):
        raise ValueError("lock set must contain exactly the three Rust wheel roots")
    plan = []
    for crate in RUST_WHEELS:
        content = (selected / f"{crate}.lock").read_bytes()
        if hashlib.sha256(content).hexdigest() != metadata["sha256"][crate]:
            raise ValueError(f"lock checksum mismatch: {crate}")
        destination = source / "packages" / crate / "Cargo.lock"
        if destination.exists() and destination.read_bytes() != content:
            raise ValueError(f"existing lock differs: {destination}")
        plan.append((destination, content))
    return plan


def restore(manifest_path: Path, source: Path, inputs_root: Path = INPUTS_ROOT) -> dict:
    manifest = json.loads(manifest_path.read_text())
    ref = manifest["components"]["xian-contracting"]["ref"]
    if not re.fullmatch(r"[0-9a-f]{40}", ref):
        raise ValueError("release source must be an immutable full commit SHA")
    check_checkout(source, ref)
    selected = inputs_root / ref
    metadata = json.loads((selected / "inputs.json").read_text())
    if metadata["contracting_ref"] != ref:
        raise ValueError("lock set source differs from the release source")
    plan = lock_plan(source, selected, metadata)
    for destination, content in plan:
        destination.write_bytes(content)
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--contracting-root", type=Path, required=True)
    parser.add_argument("--inputs-root", type=Path, default=INPUTS_ROOT)
    args = parser.parse_args()
    print(json.dumps(restore(args.manifest, args.contracting_root, args.inputs_root), indent=2))


if __name__ == "__main__":
    main()
