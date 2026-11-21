#!/usr/bin/env python3
"""Own disposable resources before either smoke flow can touch the runtime."""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import uuid
from contextlib import ExitStack
from pathlib import Path

STACK = Path(__file__).resolve().parents[1]
SOURCE_SETTINGS = {
    "XIAN_CLI_DIR",
    "XIAN_ABCI_DIR",
    "XIAN_CONFIGS_DIR",
    "XIAN_CONTRACTING_DIR",
    "XIAN_PY_DIR",
    "XIAN_COMETBFT_VERSION",
    "XIAN_S6_OVERLAY_VERSION",
}
CONTROL_SETTINGS = {
    "XIAN_SMOKE_TIMEOUT_SECONDS",
    "XIAN_SMOKE_NETWORK",
    "XIAN_SMOKE_GENESIS_SOURCE",
    "XIAN_SMOKE_GENESIS_BUNDLE",
    "XIAN_SMOKE_CHAIN_ID",
    "XIAN_SMOKE_GENESIS_TIME",
}
MAKE_SETTINGS = {
    "MAKEFLAGS",
    "MAKEOVERRIDES",
    "MFLAGS",
    "GNUMAKEFLAGS",
    "MAKELEVEL",
    "MAKEFILES",
    "CONFIGURE_ARGS",
    "DOCKER_COMPOSE",
    "BASH_ENV",
    "ENV",
}
PORTS = ("COMETBFT_RPC", "COMETBFT_P2P", "COMETBFT_METRICS", "APP_METRICS", "DASHBOARD")


def clean_environment(original: dict[str, str]) -> dict[str, str]:
    return {key: value for key, value in original.items() if keep_setting(key)}


def keep_setting(key: str) -> bool:
    if key in SOURCE_SETTINGS or key in CONTROL_SETTINGS:
        return True
    return not (key.startswith(("XIAN_", "COMPOSE_")) or key in MAKE_SETTINGS)


def available_ports() -> dict[str, str]:
    # Hold all sockets until the complete set is chosen. Docker still refuses a
    # bind if another application claims one before startup; it never replaces it.
    with ExitStack() as stack:
        sockets = [stack.enter_context(socket.socket()) for _ in PORTS]
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        return {name: str(sock.getsockname()[1]) for name, sock in zip(PORTS, sockets)}


def run_environment(root: Path, mode: str, original: dict[str, str]) -> dict[str, str]:
    env = clean_environment(original)
    project = f"xian-smoke-{mode}-{uuid.uuid4().hex}"
    paths = {
        "COMETBFT_HOME": "cometbft",
        "BDS_DATA_DIR": "bds",
        "CONTRACTS_DIR": "contracts",
        "STACK_SECRETS_ENV": "secrets.env",
        "INTENTKIT_DIR": "intentkit",
        "SMOKE_ROOT": ".",
        "SMOKE_CLI_ROOT": ".",
    }
    env.update({f"XIAN_{key}": str(root / value) for key, value in paths.items()})
    env.update(
        {
            "COMPOSE_PROJECT_NAME": project,
            "COMPOSE_DISABLE_ENV_FILE": "1",
            "DOCKER_COMPOSE": "docker compose",
            "XIAN_NODE_IMAGE_MODE": "local_build",
            "XIAN_NODE_INTEGRATED_IMAGE": f"{project}:integrated",
            "XIAN_NODE_SPLIT_IMAGE": f"{project}:split",
            "XIAN_SMOKE_CLI_NODE_NAME": "smoke-validator",
        }
    )
    for name, port in available_ports().items():
        env[f"XIAN_{name}_HOST"] = "127.0.0.1"
        env[f"XIAN_{name}_PORT"] = port
        env[f"XIAN_SMOKE_{name}_HOST"] = "127.0.0.1"
        env[f"XIAN_SMOKE_{name}_PORT"] = port
    return env


def initialize_environment(env: dict[str, str]) -> dict[str, str]:
    # Resolve the same defaults for the parent cleanup and the child. This runs
    # only after all mutable paths have been replaced with owned paths.
    output = subprocess.check_output(
        [
            "bash",
            "-e",
            "-c",
            'source "$1"; export_stack_env; env -0',
            "smoke-env",
            str(STACK / "scripts/stack-env.sh"),
        ],
        cwd=STACK,
        env=env,
    )
    return dict(item.decode().split("=", 1) for item in output.split(b"\0") if item)


def stop_child(child: subprocess.Popen) -> None:
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait()


def run_child(mode: str, env: dict[str, str]) -> int:
    with subprocess.Popen(
        ["bash", str(STACK / "scripts" / f"smoke-{mode}-run.sh")],
        cwd=STACK,
        env=env,
        start_new_session=True,
    ) as child:
        try:
            return child.wait()
        except BaseException:
            stop_child(child)
            raise


def cleanup(env: dict[str, str]) -> bool:
    # Do not run Make or consult caller settings during cleanup. These values
    # belong to this invocation and cannot be changed by the child flow.
    command = [
        "docker",
        "compose",
        "--project-name",
        env["COMPOSE_PROJECT_NAME"],
        "--profile",
        "integrated",
        "-f",
        str(STACK / "docker-compose-abci.yml"),
        "down",
        "--volumes",
        "--remove-orphans",
    ]
    if subprocess.run(command, cwd=STACK, env=env).returncode:
        return False
    images = [
        env[key]
        for key in ("XIAN_NODE_INTEGRATED_IMAGE", "XIAN_NODE_SPLIT_IMAGE")
        if subprocess.run(
            ["docker", "image", "inspect", env[key]], env=env, capture_output=True
        ).returncode
        == 0
    ]
    if images and not return_ownership(env, images[0]):
        return False
    return all(
        subprocess.run(["docker", "image", "rm", image], env=env).returncode == 0
        for image in images
    )


def return_ownership(env: dict[str, str], image: str) -> bool:
    # The node container runs as root and writes into the bind-mounted smoke
    # root; on Linux the caller could not delete those files afterwards.
    command = [
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "chown",
        "-v",
        f"{env['XIAN_SMOKE_ROOT']}:/smoke",
        image,
        "-R",
        f"{os.getuid()}:{os.getgid()}",
        "/smoke",
    ]
    return subprocess.run(command, env=env).returncode == 0


def interrupted(signum, _frame):
    # Make can forward the same signal that already reached the process group.
    # A second exception must not interrupt child termination or final cleanup.
    signal.signal(signum, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    raise SystemExit(128 + signum)


def main(mode: str) -> int:
    if mode not in {"stack", "cli"}:
        raise ValueError("smoke mode must be stack or cli")
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    root = Path(tempfile.mkdtemp(prefix=f"xian-smoke-{mode}-")).resolve()
    try:
        env = initialize_environment(run_environment(root, mode, dict(os.environ)))
    except BaseException:
        shutil.rmtree(root)
        raise
    print(
        f"Smoke resources: {root} "
        f"({env['COMPOSE_PROJECT_NAME']}, {env['XIAN_NODE_INTEGRATED_IMAGE']})",
        flush=True,
    )
    try:
        return run_child(mode, env)
    finally:
        if cleanup(env):
            shutil.rmtree(root)
        else:
            print(f"Smoke cleanup failed; retained owned resources at {root}", file=sys.stderr)
            raise SystemExit(1)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
