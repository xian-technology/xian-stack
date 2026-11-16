"""Run offline full-store tooling under the node's selected runtime image."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from node_store_runtime_guard import docker_output
from node_store_runtime_guard import require_stopped_home as check_home_runtime

STACK = Path(__file__).resolve().parents[1]


def bind(source: Path, target: str, *, readonly: bool = True) -> str:
    if any(character in str(source) for character in (",", "\n", "\r")):
        raise ValueError("Docker bind path contains unsupported characters")
    return f"type=bind,source={source},target={target}" + (",readonly" if readonly else "")


def require_stopped_home(home: Path) -> None:
    check_home_runtime(home, query=docker_output)


def inspection_limits():
    from xian.full_node_store import NativeStoreInspectionLimits

    return NativeStoreInspectionLimits.from_environment()


def inspect_store(db_dir: Path, db_backend: str, genesis_file: Path, image: str) -> dict:
    directory = db_dir.expanduser().resolve(strict=True)
    genesis = genesis_file.expanduser().resolve(strict=True)
    limits = inspection_limits()
    raw = docker_output(
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--mount",
        bind(directory, "/data"),
        "--mount",
        bind(genesis, "/genesis.json"),
        "--entrypoint",
        "xian-node-store-inspect",
        image,
        "--db-dir",
        "/data",
        "--db-backend",
        db_backend,
        "--genesis-file",
        "/genesis.json",
        *limits.command_arguments(),
        timeout=limits.timeout_seconds + 20,
    )
    return json.loads(raw)


def check_outputs(home: Path, archive: Path, manifest: Path) -> None:
    if archive == manifest:
        raise ValueError("archive and manifest outputs must differ")
    for path in (archive, manifest):
        if path.exists() or path.is_symlink():
            raise ValueError(f"snapshot output already exists: {path}")
        if path.is_relative_to(home):
            raise ValueError("snapshot outputs must be outside the source home")
        if not path.parent.is_dir():
            raise ValueError(f"snapshot output parent does not exist: {path.parent}")


def export_paths(
    home: Path, archive_output: Path, manifest_output: Path, signing_key_file: Path, offline: bool
) -> tuple:
    if offline is not True:
        raise ValueError("snapshot export requires explicit offline acknowledgement")
    home = home.expanduser().resolve(strict=True)
    archive_output = archive_output.expanduser().absolute()
    manifest_output = manifest_output.expanduser().absolute()
    archive_output = archive_output.parent.resolve(strict=True) / archive_output.name
    manifest_output = manifest_output.parent.resolve(strict=True) / manifest_output.name
    check_outputs(home, archive_output, manifest_output)
    key = signing_key_file.expanduser().resolve(strict=True)
    if key.is_relative_to(home):
        raise ValueError("snapshot signing key must be outside the exported home")
    if not key.is_file() or key.stat().st_mode & 0o077:
        raise ValueError("snapshot signing key file must be private (mode 0600)")
    return home, archive_output, manifest_output, key


def source_mounts(home: Path) -> list[str]:
    import tomllib

    runtime_home = Path("/root/.cometbft")
    mounts = [bind(home, str(runtime_home))]
    if home != runtime_home:
        mounts.append(bind(home, str(home)))
    config = home / "config/config.toml"
    if config.is_file():
        with config.open("rb") as stream:
            settings = tomllib.load(stream)
        genesis = Path(settings.get("genesis_file", "config/genesis.json"))
        if genesis.is_absolute() and genesis.is_file() and not genesis.is_relative_to(home):
            mounts.append(bind(genesis, str(genesis)))
    return mounts


def export_snapshot(
    *,
    home: Path,
    archive_output: Path,
    manifest_output: Path,
    snapshot_url: str,
    signing_key_file: Path,
    image: str,
    offline: bool,
) -> dict:
    home, archive_output, manifest_output, key = export_paths(
        home, archive_output, manifest_output, signing_key_file, offline
    )
    require_stopped_home(home)
    with tempfile.TemporaryDirectory(
        prefix=".node-store-export-", dir=archive_output.parent
    ) as work:
        mounts = [
            *source_mounts(home),
            bind(Path(work), "/work", readonly=False),
            bind(archive_output.parent, "/archive-output", readonly=False),
            bind(manifest_output.parent, "/manifest-output", readonly=False),
            bind(key, "/signing-key"),
            bind(STACK / "scripts/export_node_store_snapshot.py", "/export.py"),
        ]
        limits = inspection_limits()
        args = [
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--env",
            "XIAN_NODE_STORE_INSPECT_MAX_HISTORY_HEIGHTS=" + str(limits.max_history_heights),
            "--env",
            "XIAN_NODE_STORE_INSPECT_TIMEOUT_SECONDS=" + str(limits.timeout_seconds),
        ]
        for mount in mounts:
            args.extend(["--mount", mount])
        args.extend(
            [
                "--entrypoint",
                "python",
                image,
                "/export.py",
                "--home",
                "/root/.cometbft",
                "--source-home-alias",
                str(home),
                "--work",
                "/work",
                "--archive-output",
                "/archive-output/" + archive_output.name,
                "--manifest-output",
                "/manifest-output/" + manifest_output.name,
                "--snapshot-url",
                snapshot_url,
                "--signing-key-file",
                "/signing-key",
                "--offline",
            ]
        )
        result = json.loads(docker_output(*args, timeout=max(600, limits.timeout_seconds + 60)))
        result.update(archive_output=str(archive_output), manifest_output=str(manifest_output))
        return result


def selected_image(
    *, node_image_mode: str, node_integrated_image: str | None, node_split_image: str | None
) -> str:
    env = os.environ.copy()
    env["XIAN_NODE_IMAGE_MODE"] = node_image_mode
    if node_integrated_image:
        env["XIAN_NODE_INTEGRATED_IMAGE"] = node_integrated_image
    if node_split_image:
        env["XIAN_NODE_SPLIT_IMAGE"] = node_split_image
    result = subprocess.run(
        [
            "bash",
            "-c",
            'set -euo pipefail; source "$1"; export_stack_env; '
            'if [[ "$XIAN_STACK_TOPOLOGY" == fidelity ]]; then '
            'printf "%s" "$XIAN_NODE_SPLIT_IMAGE"; else '
            'printf "%s" "$XIAN_NODE_INTEGRATED_IMAGE"; fi',
            "bash",
            str(STACK / "scripts/stack-env.sh"),
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    image = result.stdout.strip()
    if not image:
        raise ValueError("no node image configured for snapshot tooling")
    return image


def checked_stopped_home(home: Path | None) -> dict:
    if home is None:
        raise ValueError("home is required")
    resolved = home.expanduser().resolve()
    require_stopped_home(resolved)
    return {"runtime_stopped": True, "home": str(resolved)}


def validate_snapshot_request(args) -> None:
    if args.command == "node-store-snapshot-export":
        if getattr(args, "offline", False) is not True:
            raise ValueError("snapshot export requires explicit offline acknowledgement")
        fields = ("home", "archive_output", "manifest_output", "snapshot_url", "signing_key_file")
    else:
        fields = ("db_dir", "genesis_file")
    for name in fields:
        if getattr(args, name, None) is None:
            raise ValueError(name + " is required")


def backend_snapshot(args) -> dict:
    if args.command == "node-store-snapshot-check-stopped":
        return checked_stopped_home(args.home)
    validate_snapshot_request(args)
    image = selected_image(
        node_image_mode=args.node_image_mode,
        node_integrated_image=args.node_integrated_image,
        node_split_image=args.node_split_image,
    )
    if args.command == "node-store-snapshot-inspect":
        return inspect_store(args.db_dir, args.db_backend, args.genesis_file, image)
    return export_snapshot(
        home=args.home,
        archive_output=args.archive_output,
        manifest_output=args.manifest_output,
        snapshot_url=args.snapshot_url,
        signing_key_file=args.signing_key_file,
        image=image,
        offline=args.offline,
    )
