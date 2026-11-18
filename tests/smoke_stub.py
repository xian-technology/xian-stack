"""Fake external tools for smoke lifecycle isolation tests; never contacts Docker."""

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

FIELDS = (
    "XIAN_COMETBFT_HOME",
    "XIAN_BDS_DATA_DIR",
    "XIAN_STACK_SECRETS_ENV",
    "XIAN_NODE_INTEGRATED_IMAGE",
    "COMPOSE_PROJECT_NAME",
    "XIAN_COMETBFT_RPC_HOST",
    "XIAN_COMETBFT_RPC_PORT",
    "XIAN_COMETBFT_P2P_PORT",
    "XIAN_COMETBFT_METRICS_PORT",
    "XIAN_APP_METRICS_PORT",
    "XIAN_DASHBOARD_PORT",
)


def resource(kind, name):
    return Path(os.environ["SMOKE_STUB_STATE"]) / kind / hashlib.sha256(name.encode()).hexdigest()


def change(kind, name, create):
    path = resource(kind, name)
    path.parent.mkdir(exist_ok=True)
    if create:
        path.write_text("created-by-smoke:" + name)
    else:
        path.unlink(missing_ok=True)


def record(tool, args, env):
    log = Path(os.environ["SMOKE_STUB_LOG"])
    data = {"tool": tool, "args": args, **{key: env.get(key) for key in FIELDS}}
    with log.open("a") as target:
        target.write(json.dumps(data) + "\n")


def fail(stage):
    if os.environ.get("SMOKE_STUB_FAIL") == stage:
        raise SystemExit(17)


def write_home(env):
    path = Path(env["XIAN_COMETBFT_HOME"]) / "config" / "genesis.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("smoke genesis")


def docker(args, env):
    if args[:2] == ["image", "inspect"]:
        print("sha256:" + "a" * 64)
    elif args[0] == "tag":
        change("images", args[-1], True)
    elif args[:2] == ["image", "rm"]:
        change("images", args[-1], False)
    elif "compose" in args:
        if "up" in args:
            change("projects", env["COMPOSE_PROJECT_NAME"], True)
            fail("startup")
            while os.environ.get("SMOKE_STUB_WAIT") == "1":
                time.sleep(0.05)
            time.sleep(0.15)
        elif "down" in args:
            fail("cleanup")
            change("projects", env["COMPOSE_PROJECT_NAME"], False)


def make(args, env):
    resolved = subprocess.check_output(
        [env["SMOKE_STUB_MAKE"], "--no-print-directory", "-s", "print-env"],
        env=env,
        text=True,
    )
    env.update(line.split("=", 1) for line in resolved.splitlines() if "=" in line)
    record("resolved-make", args, env)
    if "abci-build" in args:
        change("images", env["XIAN_NODE_INTEGRATED_IMAGE"], True)
        fail("build")
    elif "node-init" in args or "node-configure" in args:
        write_home(env)
    elif "node-start" in args:
        docker(["compose", "up"], env)
    elif "node-stop" in args or "abci-down" in args:
        docker(["compose", "down"], env)
    elif "node-status" in args:
        print(
            json.dumps(
                dict(
                    runtime_services_running=True,
                    required_processes_online=True,
                    backend_running=True,
                    node_id="test-node",
                )
            )
        )


def uv(args, env):
    if "xian" not in args:
        return
    command = args[args.index("xian") + 1 :]
    if command[:2] in (["network", "join"], ["network", "create"], ["node", "init"]):
        write_home(env)
        if command[0] == "network":
            base = Path(command[command.index("--base-dir") + 1]) / "nodes"
            base.mkdir(parents=True, exist_ok=True)
            profile = {
                "node_integrated_image": command[command.index("--node-integrated-image") + 1]
                if "--node-integrated-image" in command
                else None
            }
            (base / (command[2] + ".json")).write_text(json.dumps(profile))
    elif command[:2] == ["node", "start"]:
        base = Path(command[command.index("--base-dir") + 1])
        profile = json.loads((base / "nodes" / (command[2] + ".json")).read_text())
        env["XIAN_NODE_INTEGRATED_IMAGE"] = (
            profile["node_integrated_image"] or env["XIAN_NODE_INTEGRATED_IMAGE"]
        )
        env["XIAN_APP_METRICS_PORT"] = str(
            profile.get("advanced", {}).get("metrics", {}).get("port", 9108)
        )
        record("cli-runtime", command, env)
        docker(["compose", "up"], env)
        print(
            json.dumps(
                dict(rpc_checked=True, rpc_status={"result": {"node_info": {"network": "test"}}})
            )
        )
    elif command[:2] == ["node", "status"]:
        print(
            json.dumps(
                dict(
                    backend_status=dict(backend_running=True, required_processes_online=True),
                    rpc_reachable=True,
                )
            )
        )
    elif command[:2] == ["node", "stop"]:
        docker(["compose", "down"], env)


def main():
    tool = Path(sys.argv[0]).name
    args = sys.argv[1:]
    env = dict(os.environ)
    record(tool, args, env)
    {"docker": docker, "make": make, "uv": uv, "curl": lambda *_: None}[tool](args, env)


if __name__ == "__main__":
    main()
