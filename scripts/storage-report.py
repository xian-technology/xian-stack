#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path


STACK_DIR = Path(__file__).resolve().parent.parent


def env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    if raw:
        return Path(raw).expanduser().resolve()
    return default.resolve()


def nearest_existing_path(path: Path) -> Path:
    current = path
    while not current.exists():
        if current.parent == current:
            return Path("/")
        current = current.parent
    return current


def directory_size_bytes(path: Path) -> tuple[int, int]:
    if not path.exists():
        return 0, 0
    if path.is_file():
        return path.stat().st_size, 1

    total_bytes = 0
    file_count = 0
    for root, _, files in os.walk(path):
        root_path = Path(root)
        for file_name in files:
            file_path = root_path / file_name
            try:
                total_bytes += file_path.stat().st_size
                file_count += 1
            except FileNotFoundError:
                continue
    return total_bytes, file_count


def describe_path(path: Path) -> dict:
    existing_target = nearest_existing_path(path)
    filesystem = shutil.disk_usage(existing_target)
    filesystem_device = os.stat(existing_target).st_dev
    size_bytes, file_count = directory_size_bytes(path)

    return {
        "path": str(path),
        "exists": path.exists(),
        "kind": (
            "directory"
            if path.is_dir()
            else "file"
            if path.is_file()
            else "missing"
        ),
        "size_bytes": size_bytes,
        "file_count": file_count,
        "filesystem_mount_probe": str(existing_target),
        "filesystem_device": filesystem_device,
        "filesystem_total_bytes": filesystem.total,
        "filesystem_used_bytes": filesystem.used,
        "filesystem_free_bytes": filesystem.free,
    }


def main() -> int:
    cometbft_home = env_path("XIAN_COMETBFT_HOME", STACK_DIR / ".cometbft")
    xian_home = cometbft_home / "xian"
    spool_dir = env_path("XIAN_BDS_SPOOL_DIR", xian_home / "bds-spool")
    bds_data_dir = env_path("XIAN_BDS_DATA_DIR", STACK_DIR / ".bds.db")
    disk_free_warn_bytes = int(
        os.environ.get("XIAN_STACK_DISK_FREE_WARN_BYTES", "10737418240")
    )

    paths = {
        "cometbft_home": describe_path(cometbft_home),
        "xian_home": describe_path(xian_home),
        "bds_spool": describe_path(spool_dir),
        "bds_postgres_data": describe_path(bds_data_dir),
    }

    alerts: list[dict[str, object]] = []
    seen_filesystems: set[int] = set()
    for name, entry in paths.items():
        filesystem_device = int(entry["filesystem_device"])
        if filesystem_device in seen_filesystems:
            continue
        seen_filesystems.add(filesystem_device)
        probe = str(entry["filesystem_mount_probe"])
        free_bytes = int(entry["filesystem_free_bytes"])
        if free_bytes <= disk_free_warn_bytes:
            alerts.append(
                {
                    "level": "warning",
                    "code": "disk_free_low",
                    "path_key": name,
                    "filesystem_mount_probe": probe,
                    "filesystem_device": filesystem_device,
                    "threshold": disk_free_warn_bytes,
                    "value": free_bytes,
                    "message": "Low free disk space on filesystem backing Xian stack data",
                }
            )

    payload = {
        "stack_dir": str(STACK_DIR),
        "paths": paths,
        "alerts": alerts,
        "notes": [
            "Docker image layers, build cache, and container logs also consume host disk but are not measured here.",
            "The main live growth for Xian comes from CometBFT data, Xian state, the BDS spool, and Postgres data.",
        ],
    }
    json.dump(payload, fp=sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
