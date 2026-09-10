"""Shared assertions and fixtures for live blockchain lifecycle checks."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from localnet_common import compare_app_hash_window, fetch_json
from localnet_e2e_support import E2EError
from xian_py import transaction as tr
from xian_py.wallet import Wallet

STACK = Path(__file__).resolve().parents[1]

# Both integrated and split images carry these executables. Temporary observers
# run both locally, so they do not depend on an image-specific supervisor.
OBSERVER_COMMAND = """set -e
python -S - <<'PYCONFIG'
import hashlib, json, os, time
from pathlib import Path
expected = json.loads(os.environ['XIAN_E2E_CONFIG_HASHES'])
root = Path('/root/.cometbft')
deadline = time.monotonic() + 30
while True:
    try:
        ready = all(hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
                    for name, digest in expected.items())
    except OSError:
        ready = False
    if ready:
        break
    if time.monotonic() >= deadline:
        raise SystemExit('Observer configuration did not become visible inside the container')
    time.sleep(0.1)
PYCONFIG
xian-abci &
app_pid=$!
cometbft node --rpc.laddr tcp://0.0.0.0:26657 &
comet_pid=$!
trap 'kill "$app_pid" "$comet_pid" 2>/dev/null; wait' EXIT
trap 'exit 143' TERM INT
wait -n "$app_pid" "$comet_pid"
"""


def require(condition, message):
    if not condition:
        raise E2EError(message)


async def command(*args, input=None, timeout=120, combine_output=False):
    result = await asyncio.to_thread(
        subprocess.run,
        list(map(str, args)),
        input=input,
        text=True,
        capture_output=True,
        timeout=timeout,
        cwd=STACK,
    )
    require(
        result.returncode == 0,
        f"command {args[:3]} failed ({result.returncode}): {result.stderr[-3000:]}",
    )
    output = result.stdout + result.stderr if combine_output else result.stdout
    return output.strip()


async def rpc(session, node, method, **params):
    payload = await fetch_json(
        session, f"{node.rpc_url}/{method}", params={k: str(v) for k, v in params.items()}
    )
    require("result" in payload, f"{node.moniker}: {method} failed: {payload}")
    return payload["result"]


async def height(session, node):
    return int((await rpc(session, node, "status"))["sync_info"]["latest_block_height"])


async def query(session, node, path):
    response = (await rpc(session, node, "abci_query", path=json.dumps(path)))["response"]
    require(int(response.get("code", 0)) == 0, f"query {path} failed: {response}")
    raw = base64.b64decode(response.get("value") or "")
    return json.loads(raw) if raw else None


async def wait_height(session, node, target, timeout=180):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            current = await height(session, node)
            if current >= target:
                return current
        except Exception:
            pass
        await asyncio.sleep(0.5)
    raise E2EError(f"{node.moniker} did not reach height {target}")


async def agreement(session, nodes, window=5):
    # Header H+1 commits the application root produced by block H.
    target = max([await height(session, n) for n in nodes]) + 1
    await asyncio.gather(*(wait_height(session, n, target) for n in nodes))
    result = await compare_app_hash_window(session, nodes, window=window)
    require(result["ok"], "Application hashes diverged")
    # Application result fields are consensus relevant even if state roots agree.
    for h in [x["height"] for x in result["checks"]]:
        responses = [await rpc(session, n, "block_results", height=h) for n in nodes]
        canonical = [
            json.dumps(
                {
                    k: r.get(k)
                    for k in (
                        "txs_results",
                        "finalize_block_events",
                        "validator_updates",
                        "consensus_param_updates",
                    )
                },
                sort_keys=True,
            )
            for r in responses
        ]
        require(len(set(canonical)) == 1, f"Block result/fee/event divergence at height {h}")
    return result


def wallet(runner, label):
    seed = hashlib.sha256(f"{runner.seed}:{runner.run_id}:{label}".encode()).hexdigest()
    return Wallet(private_key=seed)


def prepared(runner, signer, nonce, contract, function, kwargs, chi=15000):
    return tr.prepare_transaction(
        tr.create_tx(
            {
                "chain_id": runner.network["chain_id"],
                "sender": signer.public_key,
                "nonce": nonce,
                "contract": contract,
                "function": function,
                "kwargs": kwargs,
                "chi_supplied": chi,
            },
            signer,
        )
    )


async def broadcast(session, node, tx, *, accepted=True, allow_cached=False):
    async with session.post(
        node.rpc_url,
        json={
            "jsonrpc": "2.0",
            "id": tx.tx_hash,
            "method": "broadcast_tx_sync",
            "params": {"tx": base64.b64encode(tx.submitted_bytes).decode()},
        },
        timeout=60,
    ) as response:
        payload = await response.json()
    if "error" in payload:
        text = json.dumps(payload["error"]).lower()
        if allow_cached and (
            "already exists in cache" in text or "already exists in mempool" in text
        ):
            return {"cached": True, "hash": tx.tx_hash}
        require(
            not accepted and ("too large" in text or "too big" in text),
            f"Unexpected broadcast RPC error: {payload}",
        )
        return payload
    result = payload["result"]
    require(
        (int(result.get("code", 0)) == 0) == accepted,
        f"Unexpected CheckTx outcome for {tx.tx_hash}: {result}",
    )
    return result


async def receipt(session, node, tx, *, success=True, timeout=120):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        payload = await fetch_json(
            session,
            f"{node.rpc_url}/tx",
            params={
                "hash": "0x" + tx.tx_hash,
                "prove": "false",
            },
        )
        if "result" in payload:
            result = payload["result"]
            require(
                (int(result["tx_result"].get("code", 0)) == 0) == success,
                f"Unexpected execution outcome for {tx.tx_hash}: {result['tx_result']}",
            )
            return result
        await asyncio.sleep(0.4)
    raise E2EError(f"Transaction was not committed: {tx.tx_hash}")


async def deploy(runner, session, label, source):
    name = "con_lifecycle_" + hashlib.sha256(f"{runner.run_id}:{label}".encode()).hexdigest()[:16]
    async with runner.client(runner.founder_wallet, 0, session) as client:
        existing = await client.get_contract_source(name)
        if existing is None:
            result = await client.deploy_contract(
                name=name, source=source, chi=500000, wait_for_tx=True
            )
            require(
                result.accepted and result.finalized and result.receipt.success,
                f"Failed to deploy lifecycle fixture {name}: accepted={result.accepted}, "
                f"finalized={result.finalized}, message={result.message}",
            )
    await agreement(session, runner.nodes, window=1)
    return name


def config_hashes(home):
    return json.dumps(
        {
            str(path.relative_to(home)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((home / "config").rglob("*"))
            if path.is_file()
            and path.name not in {"addrbook.json", "node_key.json", "priv_validator_key.json"}
        },
        sort_keys=True,
    )


def write_config(path, text):
    """Publish complete config files before a container reads the bind mount."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            temporary.chmod(path.stat().st_mode)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def config_path(node):
    return STACK / ".localnet" / node.moniker / ".cometbft" / "config" / "xian.toml"


def edit_toml(path, **values):
    original = path.read_text()
    updated = original
    for key, value in values.items():
        encoded = json.dumps(value)
        updated, count = re.subn(
            rf"^{re.escape(key)}\s*=.*$", f"{key} = {encoded}", updated, flags=re.MULTILINE
        )
        require(count == 1, f"Expected exactly one {key} setting in {path}")
    write_config(path, updated)
    return original


PROBE_SOURCE = """counts = Hash(default_value=0)

@export
def bump(key: str):
    counts[key] += 1
    return counts[key]

@export
def sized(blob: str):
    return len(blob)

@export
def work(rounds: int):
    counts["work-started"] += 1
    value = 0
    for i in range(rounds):
        value += i
    return value
"""


def execution(result):
    return json.loads(base64.b64decode(result["tx_result"]["data"]))
