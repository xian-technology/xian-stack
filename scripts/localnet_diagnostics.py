"""Bounded failure evidence collected before disposable localnets are removed."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from localnet_common import fetch_json


async def collect_failure_diagnostics(session, nodes, output_dir):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    async def docker(*args):
        process = await asyncio.create_subprocess_exec(
            "docker", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.communicate()
            raise
        if process.returncode:
            raise RuntimeError(stderr.decode(errors="replace")[-2000:])
        return (stdout + (stderr if args[0] == "logs" else b"")).decode(errors="replace")

    async def capture(fn):
        try:
            return await fn()
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}

    async def node_snapshot(node):
        result = {"rpc_url": node.rpc_url, "containers": {}}
        for method in ("status", "net_info", "consensus_state"):
            result[method] = await capture(
                lambda: fetch_json(session, f"{node.rpc_url}/{method}", timeout=3)
            )
        for container in dict.fromkeys((node.abci_container, node.cometbft_container)):
            if not container:
                continue
            # Exclude Config.Env, mounts, and validator key files.
            state = await capture(
                lambda: docker(
                    "inspect",
                    "--format",
                    '{"state":{{json .State}},"network":{{json .NetworkSettings}}}',
                    container,
                )
            )
            logs = await capture(lambda: docker("logs", "--tail", "200", container))
            result["containers"][container] = {"inspect": state, "logs": logs}
        return node.moniker, result

    snapshots = await asyncio.gather(*(node_snapshot(node) for node in nodes))
    path = output_dir / "nodes.json"
    path.write_text(json.dumps(dict(snapshots), indent=2) + "\n")
    return str(path)
