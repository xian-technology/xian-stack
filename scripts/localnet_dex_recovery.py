"""Launch the automation-owned crash/recovery probe against canonical DEX contracts."""

import asyncio
import os
import signal
from pathlib import Path

from localnet_e2e_support import E2EError, parse_json_stdout, write_private_json


async def run_json(command: list[str], *, cwd: Path, label: str) -> dict:
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        async with asyncio.timeout(300):
            stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise E2EError(f"{label} failed: {stderr.decode()[-2000:]}")
        payload = parse_json_stdout(stdout.decode(), label=label)
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise E2EError(f"{label} did not return a successful result")
        return payload
    finally:
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGKILL)
            await process.wait()


async def bootstrap_recovery_pool(root: Path, network: Path, suffix: str) -> dict:
    return await run_json(
        [
            "uv",
            "run",
            "--project",
            str(root / "xian-py"),
            "--with",
            str(root / "xian-contracting"),
            "python",
            str(root / "xian-stack/scripts/localnet-dex-bootstrap.py"),
            "--network-path",
            str(network),
            "--dex-bundle",
            str(root / "xian-dex/contract-bundle.json"),
            "--demo-token-contract",
            f"con_recovery_token_{suffix}",
            "--demo-lp-contract",
            f"con_recovery_lp_{suffix}",
            "--json-only",
        ],
        cwd=root / "xian-stack",
        label="canonical DEX recovery pool",
    )


async def run_recovery_smoke(repo: Path, output: Path, settings: dict) -> dict:
    script = repo / "scripts/recovery_smoke.py"
    if not script.exists():
        raise E2EError(f"DEX recovery phase requires the xian-dex-automation sibling: {script}")
    output.mkdir(parents=True, exist_ok=True)
    private_config = output / "inputs.private.json"
    write_private_json(private_config, {**settings, "output_dir": str(output)})
    try:
        payload = await run_json(
            [
                "uv",
                "run",
                "--project",
                str(repo),
                "--extra",
                "dev",
                "python",
                str(script),
                "--config",
                str(private_config),
            ],
            cwd=repo,
            label="DEX recovery probe",
        )
        if payload.get("nodes_verified") != 5:
            raise E2EError("DEX recovery probe did not confirm all five validators")
        return payload
    finally:
        private_config.unlink(missing_ok=True)
