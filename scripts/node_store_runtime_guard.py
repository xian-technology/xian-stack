#!/usr/bin/env python3
"""Prove a home has no running writable Docker mount before offline operations.

Run on the Docker host. Success authenticates the queried local daemon state;
query errors and unknown writable mount modes fail closed.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def docker_output(*args: str, timeout: float = 30) -> str:
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True, timeout=timeout
    ).stdout


def inspected_containers(raw: str, count: int) -> list:
    containers = json.loads(raw)
    if not isinstance(containers, list) or len(containers) != count:
        raise ValueError("Docker inspect did not return every queried container")
    for container in containers:
        if not isinstance(container, dict) or not isinstance(container.get("Mounts"), list):
            raise ValueError("Docker inspect has an unknown mount inventory")
    return containers


def require_stopped_home(home: Path, *, query=None) -> None:
    home = Path(home).expanduser().resolve()
    query = query or docker_output
    ids = query("ps", "--quiet").split()
    if not ids:
        return
    containers = inspected_containers(query("inspect", *ids), len(ids))
    for container in containers:
        for mount in container["Mounts"]:
            source = Path(mount["Source"]).resolve()
            overlap = home.is_relative_to(source) or source.is_relative_to(home)
            if overlap and mount.get("RW") is not False:
                raise ValueError(
                    "snapshot home overlaps a running container writable mount; stop it first"
                )


def checked_stopped_home(home: Path, *, query=None) -> dict:
    resolved = Path(home).expanduser().resolve()
    require_stopped_home(resolved, query=query)
    return {"runtime_stopped": True, "home": str(resolved)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    print(json.dumps(checked_stopped_home(parser.parse_args().home), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
