"""Replay and public signed full-node-store snapshot export/restore rehearsal.

Stage21 is part of every full five-validator E2E run. It exercises `snapshot
export` and `snapshot restore` through the current public CLI; no peer snapshot
bootstrap or node identity/signing-state import is used.
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace

from localnet_chain_checks import (
    OBSERVER_COMMAND,
    STACK,
    agreement,
    command,
    config_hashes,
    height,
    query,
    require,
    rpc,
    wait_height,
    write_config,
)
from localnet_history_checks import PolicyView, collect_policy_proof, committed_policy
from localnet_rng_checks import timestamp_nanoseconds
from xian_py.wallet import Wallet

DAY_NS = 86_400_000_000_000
IDENTITY_FILES = (
    "config/node_key.json",
    "config/priv_validator_key.json",
    "data/priv_validator_state.json",
)


def set_config(text, section, updates):
    pattern = rf"(\[{re.escape(section)}\]\n)(.*?)(?=\n\[|\Z)"
    match = re.search(pattern, text, re.DOTALL)
    require(match is not None, f"Missing config section {section}")
    body = match[2]
    for key, value in updates.items():
        body, count = re.subn(
            rf"^{re.escape(key)}\s*=.*$", f"{key} = {json.dumps(value)}", body, flags=re.MULTILINE
        )
        require(count == 1, f"Missing/duplicate config field {section}.{key}")
    return text[: match.start(2)] + body + text[match.end(2) :]


def observer_config(text, peers):
    text = set_config(text, "p2p", {"persistent_peers": peers})
    text = set_config(text, "statesync", {"enable": False})
    return re.sub(
        r"^proxy_app\s*=.*$", 'proxy_app = "unix:///tmp/abci.sock"', text, flags=re.MULTILINE
    )


def identity_fingerprint(home):
    return {name: hashlib.sha256((home / name).read_bytes()).hexdigest() for name in IDENTITY_FILES}


def restore_fingerprint(home):
    roots = [home / "config", home / "data", home / "xian"]
    return {
        str(path.relative_to(home)): hashlib.sha256(path.read_bytes()).hexdigest()
        for root in roots
        if root.exists()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def cli_environment(stack=STACK):
    env = os.environ.copy()
    sources = [str(stack.parent / "xian-cli/src"), str(stack.parent / "xian-abci/src")]
    if env.get("PYTHONPATH"):
        sources.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(sources)
    env["UV_NO_SYNC"] = "1"
    env["COMPOSE_PROJECT_NAME"] = (
        "xian-snapshot-check-" + hashlib.sha256(str(stack).encode()).hexdigest()[:12]
    )
    env["XIAN_STACK_TOPOLOGY"] = "integrated"
    return env


async def snapshot_cli(runner, base, *args, check=True):
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "xian_cli", "snapshot", *map(str, args), "--base-dir", str(base)],
        cwd=STACK,
        env=cli_environment(),
        capture_output=True,
        text=True,
        timeout=600,
    )
    if check:
        require(result.returncode == 0, f"snapshot {args[:2]} failed: {result.stderr[-3000:]}")
    return result


def write_snapshot_profiles(base, source_home, target_home, image, publisher, chain_id):
    # Public model APIs keep this fixture aligned with operator metadata.
    sys.path.insert(0, str(STACK.parent / "xian-cli/src"))
    from xian_cli.models import normalize_network_manifest, normalize_node_profile, write_json

    network = "snapshot-e2e"
    write_json(
        base / "networks" / network / "manifest.json",
        normalize_network_manifest(
            {
                "schema_version": 1,
                "name": network,
                "chain_id": chain_id,
                "genesis": {"kind": "source", "source": str(source_home / "config/genesis.json")},
            }
        ),
    )
    for name, home in (("snapshot-source", source_home), ("snapshot-target", target_home)):
        profile = {
            "schema_version": 1,
            "name": name,
            "network": network,
            "moniker": name,
            "home": str(home),
            "stack_dir": str(STACK),
            "node_image_mode": "registry",
            "node_integrated_image": image,
            "node_split_image": image,
            "snapshot_signing_keys": [publisher],
            "services": {
                "bds": {"enabled": False},
                "dashboard": {"enabled": False},
                "monitoring": {"enabled": False},
            },
        }
        write_json(base / "nodes" / (name + ".json"), normalize_node_profile(profile))


PUBLIC_PATCH_FIELDS = {
    "version",
    "patch_id",
    "activation_height",
    "governance_contract",
    "summary",
    "uri",
    "chain_id",
    "changes",
    "bundle_hash",
}


def public_state_patch_files(directory):
    require(not directory.is_symlink(), "Public state-patch inventory must not be a link")
    require(directory.is_dir(), "Public state-patch inventory must be a directory")
    files = []
    for path in sorted(directory.iterdir()):
        require(
            not path.is_symlink() and path.is_file(),
            "Public state-patch entries must be regular files",
        )
        if path.suffix == ".json":
            require(
                path.name not in {Path(name).name for name in IDENTITY_FILES},
                "Node identity is not a public state-patch bundle",
            )
            files.append(path)
    return files


def validated_public_state_patch_inventory(directory, chain_id):
    from xian.utils.state_patches import StatePatchManager

    files = public_state_patch_files(directory)
    manager = StatePatchManager(None, chain_id=chain_id)
    manager.load_patches(directory)
    inventory = []
    for path in files:
        raw = path.read_bytes()
        payload = json.loads(raw)
        require(not set(payload) - PUBLIC_PATCH_FIELDS, "State-patch bundle has unrelated metadata")
        bundle = manager.local_bundles[payload["patch_id"]]
        require(bundle.chain_id in {None, chain_id}, "State-patch bundle targets another chain")
        require(
            payload.get("bundle_hash", bundle.bundle_hash) == bundle.bundle_hash,
            "State-patch bundle declares an incorrect hash",
        )
        inventory.append(
            {
                "filename": path.name,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "patch_id": bundle.patch_id,
                "bundle_hash": bundle.bundle_hash,
                "activation_height": bundle.activation_height,
            }
        )
    return inventory


def copy_public_state_patch_inventory(source_home, home):
    source = source_home / "config/state-patches"
    require(not source.is_symlink(), "Public state-patch inventory must not be a link")
    if not source.exists():
        return []
    chain_id = json.loads((source_home / "config/genesis.json").read_text())["chain_id"]
    expected = validated_public_state_patch_inventory(source, chain_id)
    destination = home / "config/state-patches"
    destination.mkdir()
    try:
        for record in expected:
            raw = (source / record["filename"]).read_bytes()
            require(
                hashlib.sha256(raw).hexdigest() == record["sha256"],
                "Public state-patch inventory changed while copying",
            )
            (destination / record["filename"]).write_bytes(raw)
        installed = validated_public_state_patch_inventory(destination, chain_id)
        require(installed == expected, "Copied public state-patch inventory does not match")
    except BaseException:
        shutil.rmtree(destination)
        raise
    return installed


async def prepare_home(source_home, home, image, peers):
    require(not home.exists(), f"Observer home already exists: {home}")
    (home / "config").mkdir(parents=True)
    for name in ("config.toml", "genesis.json", "xian.toml"):
        shutil.copy2(source_home / "config" / name, home / "config" / name)
    inventory = copy_public_state_patch_inventory(source_home, home)
    await command(
        "docker",
        "run",
        "--rm",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--entrypoint",
        "cometbft",
        "--volume",
        f"{home}:/node-home",
        image,
        "init",
        "--home",
        "/node-home",
    )
    config = home / "config/config.toml"
    write_config(config, observer_config(config.read_text(), peers))
    runtime = home / "config/xian.toml"
    write_config(
        runtime,
        re.sub(
            r"^bds_enabled\s*=.*$", "bds_enabled = false", runtime.read_text(), flags=re.MULTILINE
        ),
    )
    return inventory


async def observer_address(container, mode):
    info = json.loads(await command("docker", "inspect", container))[0]
    ports = info["NetworkSettings"]["Ports"].get("26657/tcp")
    require(info["State"]["Running"] and ports, f"Observer {mode} exited before RPC startup")
    return SimpleNamespace(moniker=mode, rpc_url="http://127.0.0.1:" + ports[0]["HostPort"])


async def create_observer(home, container, image, network, mode):
    await command(
        "docker",
        "create",
        "--entrypoint",
        "/bin/bash",
        "--name",
        container,
        "--network",
        network,
        "--publish",
        "127.0.0.1::26657",
        "--memory",
        "1536m",
        "--env",
        f"XIAN_E2E_CONFIG_HASHES={config_hashes(home)}",
        "--volume",
        f"{home}:/root/.cometbft",
        image,
        "-c",
        OBSERVER_COMMAND,
    )
    try:
        await command("docker", "start", container)
        return await observer_address(container, mode)
    except BaseException:
        with contextlib.suppress(Exception):
            await command("docker", "rm", "-f", "-v", container)
        raise


async def common_block_proof(session, nodes, consensus):
    common_height = max(item["height"] for item in consensus["checks"])
    blocks = [await rpc(session, node, "block", height=common_height) for node in nodes]
    hashes = {block["block_id"]["hash"] for block in blocks}
    require(len(hashes) == 1, "Observer block identities diverged")
    return {
        "height": common_height,
        "block_hash": blocks[0]["block_id"]["hash"],
        "header": blocks[0]["block"]["header"],
    }


async def observer_agreement(runner, session, observer):
    await wait_height(session, observer, await height(session, runner.nodes[0]), timeout=300)
    status = await rpc(session, observer, "status")
    require(
        int(status["validator_info"]["voting_power"]) == 0, "Observer unexpectedly has voting power"
    )
    consensus = await agreement(session, [*runner.nodes, observer], window=3)
    for key in (
        "validators.current",
        "governance.proposal_count",
        f"__n.{runner.founder_wallet.public_key}:",
    ):
        expected = await query(session, runner.nodes[0], "/get/" + key)
        require(
            await query(session, observer, "/get/" + key) == expected,
            f"Observer state mismatch at {key}",
        )
    return {
        "consensus": consensus,
        "voting_power": 0,
        "common_block": await common_block_proof(session, [*runner.nodes, observer], consensus),
    }


async def stop_backup_source(container):
    # Let stock Comet close after its in-flight ABCI work; only then does the
    # observer supervisor's wait-n/EXIT path stop the application process.
    await command("docker", "exec", container, "pkill", "-TERM", "-x", "cometbft")
    await command("docker", "wait", container, timeout=60)
    state = json.loads(await command("docker", "inspect", container))[0]["State"]
    require(
        not state["Running"] and not state.get("OOMKilled"), "Backup source did not stop cleanly"
    )


async def remove_observer(runner, container, mode):
    logs = await command("docker", "logs", container, combine_output=True)
    (runner.output_dir / f"observer-{mode}.log").write_text(logs)
    await command("docker", "rm", "-f", "-v", container)


async def rejected_restore(runner, target, manifest_url, label):
    before = restore_fingerprint(target)
    result = await snapshot_cli(
        runner,
        runner.output_dir / "snapshot-cli",
        "restore",
        "snapshot-target",
        "--snapshot-url",
        manifest_url,
        check=False,
    )
    require(result.returncode != 0, f"{label} restore unexpectedly succeeded")
    require(
        restore_fingerprint(target) == before,
        f"{label} restore published application data or changed local identity",
    )
    error = (result.stderr + result.stdout)[-2000:]
    reasons = {
        "tampered-archive": ("sha256", "checksum", "byte count", "byte limit"),
        "wrong-chain": ("chain_id",),
        "incomplete-native-store": ("native", "blockstore"),
    }
    if label in reasons:
        require(
            any(word in error.lower() for word in reasons[label]),
            f"{label} failed for an unrelated reason: {error}",
        )
    return {
        "case": label,
        "rejected": True,
        "returncode": result.returncode,
        "error": error,
    }


def sign_manifest(manifest, wallet, path):
    from xian.node_admin import full_node_store_manifest_payload

    manifest = {**manifest, "signing_public_key": wallet.public_key}
    manifest["signature"] = wallet.sign_msg(full_node_store_manifest_payload(manifest))
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return path.as_uri()


def archive_metadata(path):
    with gzip.open(path, "rb") as stream:
        raw_bytes = sum(len(chunk) for chunk in iter(lambda: stream.read(1024 * 1024), b""))
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {
        "snapshot_url": path.as_uri(),
        "snapshot_sha256": digest,
        "snapshot_bytes": path.stat().st_size,
        "uncompressed_bytes": raw_bytes,
    }


def negative_manifests(archive, manifest, wallet, output):
    tampered = output / "tampered.tar.gz"
    shutil.copy2(archive, tampered)
    with tampered.open("r+b") as stream:
        offset = tampered.stat().st_size // 2
        stream.seek(offset)
        value = stream.read(1)
        require(bool(value), "Cannot tamper an empty snapshot archive")
        stream.seek(offset)
        stream.write(bytes([value[0] ^ 1]))
    bad_sha = {**manifest, "snapshot_url": tampered.as_uri()}
    wrong_chain = {**manifest, "chain_id": manifest["chain_id"] + "-wrong"}
    incomplete = output / "incomplete.tar.gz"
    with tarfile.open(archive, "r:gz") as source, tarfile.open(incomplete, "w:gz") as target:
        for member in source:
            if member.name == "data/blockstore.db" or member.name.startswith("data/blockstore.db/"):
                continue
            target.addfile(member, source.extractfile(member) if member.isfile() else None)
    missing_store = {**manifest, **archive_metadata(incomplete)}
    variants = {
        "tampered-archive": bad_sha,
        "wrong-chain": wrong_chain,
        "incomplete-native-store": missing_store,
    }
    return {
        label: sign_manifest(value, wallet, output / (label + ".json"))
        for label, value in variants.items()
    }


async def wait_validator_ready(runner, session, target):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        async with runner.client(runner.validator_wallets[0], 0, session) as client:
            record = await client.call("validators", "get_validator", {"account": target})
        if record.get("pending_slash_count") == 0 and record.get("jailed") is True:
            return record
        await asyncio.sleep(0.5)
    raise RuntimeError("Snapshot evidence penalty did not finish before validator recovery")


async def governance_vote(runner, session, kind, arg, label):
    async with contextlib.AsyncExitStack() as stack:
        clients = [
            await stack.enter_async_context(runner.client(wallet, index, session))
            for index, wallet in enumerate(runner.validator_wallets[:4])
        ]
        vote = await runner.approve_members_vote(
            clients[0],
            [(f"node{index}", client) for index, client in enumerate(clients[1:], start=1)],
            type_of_vote=kind,
            arg=arg,
            label_prefix=label,
        )
    heights = [item.get("height", 0) for item in [vote["proposal_receipt"], *vote["vote_receipts"]]]
    await asyncio.gather(*(wait_height(session, node, max(heights) + 1) for node in runner.nodes))
    return vote


async def validator_selection(runner, session):
    async with runner.client(runner.validator_wallets[0], 0, session) as client:
        return await client.call("validators", "get_active_validators", {})


async def verify_native_selection(session, nodes, expected, consensus):
    wanted = {
        hashlib.sha256(bytes.fromhex(item["account"])).hexdigest()[:40].upper(): int(item["power"])
        for item in expected
    }
    common_height = max(item["height"] for item in consensus["checks"])
    responses = [
        await rpc(session, node, "validators", height=common_height, per_page=100) for node in nodes
    ]
    powers = [
        {entry["address"]: int(entry["voting_power"]) for entry in response["validators"]}
        for response in responses
    ]
    require(
        all(value == wanted for value in powers),
        "Restored native validator powers differ from application selection",
    )
    return powers


async def wait_selection(runner, session, expected):
    deadline = time.monotonic() + 90
    wanted = {item["account"]: item["power"] for item in expected}
    while time.monotonic() < deadline:
        values = await validator_selection(runner, session)
        if {item["account"]: item["power"] for item in values} == wanted:
            consensus = await agreement(session, runner.nodes, window=3)
            await verify_native_selection(session, runner.nodes, expected, consensus)
            return values
        await asyncio.sleep(0.5)
    raise RuntimeError("Validator selection did not recover after snapshot evidence rehearsal")


async def restore_evidence_validator(runner, session, before, target):
    record = await wait_validator_ready(runner, session, target)
    unjail = await governance_vote(runner, session, "unjail_member", target, "snapshot-unjail")
    active = await validator_selection(runner, session)
    add = None
    if target not in {item["account"] for item in active}:
        add = await governance_vote(runner, session, "add_member", target, "snapshot-readd")
    return {
        "penalty_completed": record,
        "unjail": unjail,
        "add_member": add,
        "restored_active": await wait_selection(runner, session, before),
    }


def protocol_module():
    name = "snapshot_protocol_safety"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, STACK / "scripts/localnet-protocol-safety.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


async def historical_evidence(runner, session, observer, checkpoint):
    protocol = protocol_module()
    helper = object.__new__(protocol.ProtocolSafetyRunner)
    helper.nodes, helper.network = runner.nodes, runner.network
    before = await validator_selection(runner, session)
    target = runner.nodes[4].account_public_key
    require(
        target in {item["account"] for item in before},
        "Snapshot evidence target node4 must be active",
    )
    start = await height(session, runner.nodes[0])
    infraction = checkpoint - 1
    evidence = await helper.broadcast_duplicate_vote_evidence(
        session,
        reporter_node_index=0,
        target_node_index=4,
        evidence_height=infraction,
    )
    require(
        "error" not in evidence["response"],
        f"Snapshot historical proof rejected: {evidence['response']}",
    )
    included = await find_evidence_block(session, runner.nodes[0], start, infraction)
    nodes = [*runner.nodes, observer]
    await asyncio.gather(*(wait_height(session, node, included + 3) for node in nodes))
    results = [await rpc(session, node, "block_results", height=included) for node in nodes]
    require(
        len({json.dumps(value, sort_keys=True) for value in results}) == 1,
        "Historical evidence block results differ across restored peer",
    )
    headers = [
        (await rpc(session, node, "block", height=included + 1))["block"]["header"]
        for node in nodes
    ]
    require(
        len({header["last_results_hash"] for header in headers}) == 1,
        "Historical evidence LastResultsHash differs",
    )
    check = await agreement(session, nodes, window=3)
    recovery = await restore_evidence_validator(runner, session, before, target)
    return {
        "infraction_height": infraction,
        "evidence": evidence,
        "included_height": included,
        "exact_results": results,
        "next_headers": headers,
        "consensus": check,
        "validator_recovery": recovery,
    }


async def find_evidence_block(session, node, start, infraction):
    deadline = time.monotonic() + 120
    checked = start
    while time.monotonic() < deadline:
        latest = await height(session, node)
        for candidate in range(checked, latest + 1):
            block = await rpc(session, node, "block", height=candidate)
            items = block["block"].get("evidence", {}).get("evidence") or []
            if any(
                int(item.get("value", {}).get("vote_a", {}).get("height", 0)) == infraction
                for item in items
            ):
                return candidate
        checked = latest + 1
        await asyncio.sleep(0.5)
    raise RuntimeError("Accepted historical evidence was not committed")


SCHEDULE_FIELDS = {
    "version",
    "max_age_num_blocks",
    "base_max_age_duration",
    "max_bytes",
    "target_max_age_duration",
    "growth_start_nanos",
    "security_checkpoint_height",
    "security_checkpoint_nanos",
    "previous_security_checkpoint_height",
}


def security_window(schedule, view):
    current = schedule["security_checkpoint_height"]
    previous = schedule["previous_security_checkpoint_height"]
    require(
        type(view.height) is int and 0 <= current <= view.height <= 2**63 - 1,
        "Invalid authenticated security checkpoint height",
    )
    require(
        previous is None or (type(previous) is int and 0 <= previous <= current),
        "Invalid previous security checkpoint height",
    )
    require(
        schedule["security_checkpoint_nanos"] <= view.last_block_nanos,
        "Security checkpoint time exceeds committed time",
    )
    configured = schedule["max_age_num_blocks"]
    require(0 < configured <= 2**63 - 1, "Invalid configured evidence block window")
    floor = previous if previous is not None else current
    effective = max(configured, view.height - floor)
    return effective, view.height - effective


def retention_state(schedule, view):
    require(
        isinstance(schedule, dict) and set(schedule) == SCHEDULE_FIELDS,
        "Snapshot retention requires the exact authenticated schedule fields",
    )
    require(
        all(
            type(schedule[key]) is int
            for key in SCHEDULE_FIELDS - {"previous_security_checkpoint_height"}
        )
        and schedule["version"] == 2,
        "Snapshot retention requires canonical schema2 schedule integers",
    )
    nanos = view.last_block_nanos
    start = schedule["growth_start_nanos"]
    require(nanos >= start, "Committed retention time precedes growth start")
    duration = min(
        schedule["target_max_age_duration"], schedule["base_max_age_duration"] + nanos - start
    )
    blocks, block_cutoff = security_window(schedule, view)
    expected = {
        "max_age_num_blocks": str(blocks),
        "max_age_duration": str(duration),
        "max_bytes": str(schedule["max_bytes"]),
    }
    require(
        dict(view.evidence) == expected, "Committed retention differs from authenticated schedule"
    )
    return {
        "schedule": dict(schedule),
        "height": view.height,
        "app_hash": view.app_hash,
        "last_block_nanos": nanos,
        "evidence": dict(view.evidence),
        "effective_duration": duration,
        "cutoff_nanos": nanos - duration,
        "block_cutoff_height": block_cutoff,
        "pending": duration < schedule["target_max_age_duration"],
    }


async def retention_sample(session, node):
    schedule = await query(session, node, "/get/__evidence_policy")
    view, _response = await committed_policy(session, node)
    return retention_state(schedule, view)


def require_retention_progress(before, after):
    require(
        after["height"] >= before["height"]
        and after["last_block_nanos"] >= before["last_block_nanos"],
        "Committed retention clock moved backward",
    )
    require(
        after["effective_duration"] >= before["effective_duration"],
        "Effective retention shrank after restart or policy change",
    )
    require(after["cutoff_nanos"] >= before["cutoff_nanos"], "Evidence cutoff moved backward")
    require(
        after["block_cutoff_height"] >= before["block_cutoff_height"],
        "Block evidence cutoff moved backward",
    )


def require_growth_schedule(before, after):
    old, new = before["schedule"], after["schedule"]
    require(
        new["target_max_age_duration"] > old["target_max_age_duration"],
        "Snapshot retention target did not increase",
    )
    require(
        new["growth_start_nanos"] >= before["last_block_nanos"],
        "New retention growth started before the observed committed clock",
    )
    old_at_start = min(
        old["target_max_age_duration"],
        old["base_max_age_duration"] + new["growth_start_nanos"] - old["growth_start_nanos"],
    )
    require(
        new["base_max_age_duration"] == old_at_start,
        "Retention raise banked idle time toward a new target",
    )
    for name in ("max_age_num_blocks", "max_bytes"):
        require(new[name] == old[name], "Configured evidence limit changed during retention raise")


def policy_approval_height(vote):
    receipts = [vote["proposal_receipt"], *vote["vote_receipts"]]
    heights = [
        int(receipt["height"])
        for receipt in receipts
        if any(
            event.get("contract") == "validators"
            and event.get("event") == "ValidatorProposalApproved"
            and event.get("data_indexed", {}).get("proposal_id") == vote["proposal_id"]
            for event in receipt.get("events", [])
        )
    ]
    require(len(heights) == 1, "Retention raise requires one exact policy approval receipt")
    return heights[0]


async def growth_start_proof(session, node, increase, state):
    height = policy_approval_height(increase)
    block = await rpc(session, node, "block", height=height)
    header = block["block"]["header"]
    require(
        header["height"] == str(height)
        and state["schedule"]["growth_start_nanos"] == timestamp_nanoseconds(header["time"]),
        "Retention growth anchor differs from the approving block timestamp",
    )
    return {"height": height, "header": header}


async def cadence_rehearsal(runner, session, **options):
    # Parent-owned cadence helper is imported only for its actual live substage.
    from localnet_cadence_checks import cadence_rehearsal as rehearse

    return await rehearse(runner, session, **options)


async def retention_rehearsal(runner, session):
    async with runner.client(runner.validator_wallets[0], 0, session) as client:
        old_days = await client.get_state("validators", "config", "unbonding_period_days")
    before = await retention_sample(session, runner.nodes[0])
    target = before["schedule"]["target_max_age_duration"]
    new_days = target // DAY_NS + 1
    require(
        new_days <= 106750, "Snapshot retention rehearsal cannot exceed the supported policy range"
    )
    increase = await governance_vote(
        runner,
        session,
        "update_policy",
        {"unbonding_period_days": new_days},
        "snapshot-retention-increase",
    )
    grown = await collect_policy_proof(session, runner.nodes[0])
    current = await retention_sample(session, runner.nodes[0])
    require_growth_schedule(before, current)
    require_retention_progress(before, current)
    require(
        current["effective_duration"] > before["effective_duration"],
        "Effective retention did not grow after the policy increase",
    )
    start_proof = await growth_start_proof(session, runner.nodes[0], increase, current)
    require(current["pending"], "Retention rehearsal must remain mid-ramp")
    cadence = await cadence_rehearsal(
        runner, session, label="pending-retention", expected_pending=True
    )
    decrease = await governance_vote(
        runner,
        session,
        "update_policy",
        {"unbonding_period_days": old_days},
        "snapshot-retention-restore",
    )
    reduced = await retention_sample(session, runner.nodes[0])
    require(
        reduced["schedule"] == current["schedule"],
        "Retention high-water or authenticated schedule changed after policy reduction",
    )
    require_retention_progress(current, reduced)
    require(reduced["pending"], "Full-store backup must retain a pending growth schedule")
    return {
        "original_days": old_days,
        "raised_days": new_days,
        "target_before": target,
        "target_after": reduced["schedule"]["target_max_age_duration"],
        "schedule_after": reduced["schedule"],
        "initial_state": before,
        "growth_state": current,
        "restored_state": reduced,
        "increase_vote": increase,
        "restore_vote": decrease,
        "growth_proof": grown,
        "growth_start_proof": start_proof,
        "reduction_proof": await collect_policy_proof(session, runner.nodes[0]),
        "cadence": cadence,
    }


async def repeated_restart(runner, session, container, observer, retention):
    proof = []
    for attempt in range(2):
        before = await retention_sample(session, observer)
        await command("docker", "restart", "--time", "30", container)
        observer = await observer_address(container, "full-node-store")
        consensus = await observer_agreement(runner, session, observer)
        after = await retention_sample(session, observer)
        require(
            after["schedule"]["target_max_age_duration"] == retention["target_after"],
            "Restored retention target changed after restart",
        )
        require(
            before["schedule"] == after["schedule"] == retention["schedule_after"],
            "Authenticated retention schedule changed after restart",
        )
        require_retention_progress(before, after)
        proof.append(
            {
                "attempt": attempt + 1,
                "consensus": consensus,
                "before": before,
                "after": after,
                "evidence_policy": await collect_policy_proof(session, observer),
            }
        )
    return proof


async def checkpoint_retention(runner, session, manifest, retention):
    node = runner.nodes[0]
    height = manifest["height"]
    await wait_height(session, node, height + 1)
    block, next_block, parameters = await asyncio.gather(
        rpc(session, node, "block", height=height),
        rpc(session, node, "block", height=height + 1),
        rpc(session, node, "consensus_params", height=height + 1),
    )
    header, next_header = block["block"]["header"], next_block["block"]["header"]
    require(
        header["height"] == str(height)
        and next_header["height"] == str(height + 1)
        and int(parameters["block_height"]) == height + 1,
        "Snapshot checkpoint response heights do not match the signed checkpoint height",
    )
    require(
        block["block_id"]["hash"].lower() == manifest["block_hash"].lower(),
        "Snapshot checkpoint block differs from live canonical history",
    )
    require(
        next_header["app_hash"].lower() == manifest["app_hash"].lower(),
        "Snapshot checkpoint application root differs from the next header",
    )
    state = retention_state(
        retention["schedule_after"],
        PolicyView(
            height=height,
            app_hash=manifest["app_hash"],
            last_block_nanos=timestamp_nanoseconds(header["time"]),
            evidence=parameters["consensus_params"]["evidence"],
        ),
    )
    require(state["pending"], "Full-store checkpoint must be mid-ramp")
    return {
        "state": state,
        "header": header,
        "next_header": next_header,
        "consensus_params": parameters,
    }


async def restored_retention_state(session, observer, checkpoint):
    state = await retention_sample(session, observer)
    before = checkpoint["state"]
    require(
        state["schedule"] == before["schedule"], "Full-store restore changed retention schedule"
    )
    require_retention_progress(before, state)
    require(state["pending"], "Full-store restored peer must remain mid-ramp")
    return state


async def snapshot_export(runner, source_home, target_home, image, backup):
    wallet = Wallet()
    key = backup / "publisher.key"
    key.write_text(wallet.private_key)
    key.chmod(0o600)
    base = runner.output_dir / "snapshot-cli"
    write_snapshot_profiles(
        base, source_home, target_home, image, wallet.public_key, runner.network["chain_id"]
    )
    archive = backup / "full-node-store.tar.gz"
    manifest_path = backup / "manifest.json"
    await snapshot_cli(
        runner,
        base,
        "export",
        "snapshot-source",
        "--archive-output",
        archive,
        "--manifest-output",
        manifest_path,
        "--snapshot-url",
        archive.as_uri(),
        "--signing-key-file",
        key,
    )
    manifest = json.loads(manifest_path.read_text())
    require(
        manifest["manifest_version"] == 2 and manifest["snapshot_kind"] == "full_node_store",
        "CLI did not export a verified full-node-store manifest",
    )
    negatives = negative_manifests(archive, manifest, wallet, backup)
    return manifest_path, manifest, negatives


def advance_recipient_signing_state(home, checkpoint):
    state = home / "data/priv_validator_state.json"
    value = json.loads(state.read_text())
    value["height"] = str(checkpoint + 1000)
    state.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    state.chmod(0o600)


async def restore_snapshot(runner, target_home, manifest_path, negatives):
    rejected = []
    for label, url in negatives.items():
        rejected.append(await rejected_restore(runner, target_home, url, label))
    identities = identity_fingerprint(target_home)
    await snapshot_cli(
        runner,
        runner.output_dir / "snapshot-cli",
        "restore",
        "snapshot-target",
        "--snapshot-url",
        manifest_path.as_uri(),
    )
    require(
        identity_fingerprint(target_home) == identities,
        "Full-store restore imported another node's identity or signing high-water",
    )
    return rejected


async def full_store_rehearsal(runner, session, image, network, source_home, peers):
    replay_home = runner.output_dir / "observers/replay"
    target_home = runner.output_dir / "observers/full_node_store"
    replay_inventory = await prepare_home(source_home, replay_home, image, peers)
    target_inventory = await prepare_home(source_home, target_home, image, peers)
    require(
        replay_inventory and replay_inventory == target_inventory,
        "Replay requires matching historical public state-patch inventories",
    )
    replay_container = f"xian-e2e-{runner.run_id.lower()}-replay"
    restored_container = f"xian-e2e-{runner.run_id.lower()}-full-store"
    created = []
    try:
        replay = await create_observer(replay_home, replay_container, image, network, "replay")
        created.append((replay_container, "replay"))
        replay_proof = await observer_agreement(runner, session, replay)
        retention = await retention_rehearsal(runner, session)
        await observer_agreement(runner, session, replay)
        await stop_backup_source(replay_container)
        backup = runner.output_dir / "node-store-backup"
        backup.mkdir()
        manifest_path, manifest, negatives = await snapshot_export(
            runner, replay_home, target_home, image, backup
        )
        checkpoint = await checkpoint_retention(runner, session, manifest, retention)
        advance_recipient_signing_state(target_home, manifest["height"])
        rejected = await restore_snapshot(runner, target_home, manifest_path, negatives)
        restored = await create_observer(
            target_home, restored_container, image, network, "full-node-store"
        )
        created.append((restored_container, "full-node-store"))
        restored_proof = await observer_agreement(runner, session, restored)
        restored_retention = await restored_retention_state(session, restored, checkpoint)
        evidence = await historical_evidence(runner, session, restored, manifest["height"])
        restarts = await repeated_restart(runner, session, restored_container, restored, retention)
        return {
            "snapshot": manifest,
            "public_state_patch_bundles": {
                "replay": replay_inventory,
                "full_node_store": target_inventory,
            },
            "observers": [
                {"mode": "replay", **replay_proof},
                {"mode": "full_node_store", **restored_proof},
            ],
            "negative_restores": rejected,
            "retention": retention,
            "retention_checkpoint": checkpoint,
            "restored_retention": restored_retention,
            "historical_evidence": evidence,
            "restarts": restarts,
        }
    finally:
        for container, mode in reversed(created):
            await remove_observer(runner, container, mode)


async def sync_phase(runner, session):
    source = runner.nodes[0]
    info = json.loads(await command("docker", "inspect", source.abci_container))[0]
    image = info["Image"]
    network = next(iter(info["NetworkSettings"]["Networks"]))
    source_home = STACK / ".localnet" / source.moniker / ".cometbft"
    peers = ",".join(
        f"{node['node_id'].lower()}@{node['moniker']}:26656" for node in runner.network["nodes"]
    )
    return await full_store_rehearsal(runner, session, image, network, source_home, peers)
