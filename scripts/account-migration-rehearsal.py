#!/usr/bin/env python3
"""Isolated five-validator account migration/recovery drill using current sources.

Run with the ABCI development Python (with xian-py[hybrid] available). Never
reads/writes .localnet or existing nodes. Enrollment is enabled only in the
fresh disposable genesis. All child processes are stopped on success/failure.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHAIN = "xian-account-rehearsal-local"
NODES = 5


def node_main(home: Path):
    from abci.server import ABCIServer
    from xian.app_logging import configure_logging
    from xian.constants import Constants
    from xian.xian_abci import Xian

    class LocalConstants(Constants):
        COMETBFT_HOME = home
        COMETBFT_CONFIG = home / "config/config.toml"
        XIAN_CONFIG = home / "config/xian.toml"
        COMETBFT_GENESIS = home / "config/genesis.json"
        STORAGE_HOME = home / "xian"

    constants = LocalConstants()
    configure_logging(constants)
    app = asyncio.run(Xian.create(constants=constants))
    ABCIServer(app, socket_path=app.cometbft_config["proxy_app"].removeprefix("unix://")).run()


def wait_for(check, *, timeout=90, label="condition"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except (OSError, ValueError, KeyError) as exc:
            last = str(exc)
        time.sleep(0.25)
    raise RuntimeError(f"timed out waiting for {label}: {last}")


def durable_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as handle:
        json.dump(value, handle, indent=2, default=str)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def source_identity(repo):
    root = ROOT.parent / repo
    paths = (
        subprocess.check_output(
            ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"]
        )
        .decode()
        .split("\0")
    )
    digest = hashlib.sha256()
    for name in sorted(set(paths) - {""}):
        path = root / name
        if path.is_file():
            digest.update(name.encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return {
        "commit": subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
        ).strip(),
        "working_tree_sha256": digest.hexdigest(),
    }


class Rehearsal:
    def __init__(self, output: Path, binary: str, base_port: int, *, enrollment_open=True):
        self.output, self.binary, self.base_port = output, binary, base_port
        self.enrollment_open = enrollment_open
        output.mkdir(parents=True, mode=0o700, exist_ok=False)
        self.processes = {}
        self.logs = []
        self.steps = []
        self.password = secrets.token_hex(24)
        self.report = {
            "chain_id": CHAIN,
            "validators": NODES,
            "steps": self.steps,
            "default_enrollment_changed": False,
            "cometbft_version": subprocess.check_output([binary, "version"], text=True).strip(),
        }

        self.report["cometbft_binary_sha256"] = hashlib.sha256(
            Path(binary).read_bytes()
        ).hexdigest()
        if shutil.which("go"):
            build_info = subprocess.check_output(["go", "version", "-m", binary], text=True)
            for line in build_info.splitlines():
                fields = line.split()
                if len(fields) >= 3 and fields[:2] == ["mod", "github.com/cometbft/cometbft"]:
                    self.report["cometbft_module"] = {
                        "version": fields[2],
                        "checksum": fields[3] if len(fields) > 3 else None,
                    }

    def mark(self, name, **details):
        print(f"[account-rehearsal] {name}", flush=True)
        self.steps.append({"name": name, **details})
        self.save_report()

    def save_report(self):
        durable_json(self.output / "report.json", self.report)

    def rpc(self, method, params=None, node=0):
        data = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        ).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.base_port + node * 10 + 1}",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            result = json.load(response)
        if "error" in result:
            raise ValueError(result["error"])
        return result["result"]

    def query(self, key, node=0):
        result = self.rpc("abci_query", {"path": "/get/" + key}, node)["response"]
        if int(result.get("code", 0)):
            raise ValueError(result)
        if result.get("value") is None and not key.startswith("_accounts."):
            return None
        value = base64.b64decode(result["value"])
        if value == b"\0":
            return None
        from xian_runtime_types.encoding import decode

        try:
            decoded = decode(value.decode())
            if decoded is None and value != b"null":
                if key.startswith("_accounts."):
                    raise ValueError("malformed registry JSON")
                return value.decode()
            return decoded
        except (ValueError, TypeError):
            if key.startswith("_accounts."):
                raise
            return value.decode()

    def nonce(self, sender):
        result = self.rpc("abci_query", {"path": "/get_next_nonce/" + sender})["response"]
        return json.loads(base64.b64decode(result["value"]))

    def payload(self, sender, contract, function, kwargs):
        return dict(
            sender=sender,
            chain_id=CHAIN,
            nonce=self.nonce(sender),
            chi_supplied=100000,
            contract=contract,
            function=function,
            kwargs=kwargs,
        )

    def broadcast(self, tx, *, expect=0, node=0, wait=True):
        from xian_py.transaction import prepare_transaction

        prepared = prepare_transaction(tx)
        try:
            result = self.rpc(
                "broadcast_tx_sync",
                {"tx": base64.b64encode(prepared.submitted_bytes).decode()},
                node,
            )
        except ValueError as exc:
            if expect or not wait or "tx already exists in cache" not in str(exc):
                raise
            # Gossip may restore the exact pending transaction before our retry.
            # A cache hit is not finality: still wait for its committed receipt.
            result = {"code": 0}
        assert int(result.get("code", 0)) == expect, result
        if expect or not wait:
            return prepared.tx_hash
        receipt = wait_for(
            lambda: self.rpc(
                "tx", {"hash": base64.b64encode(bytes.fromhex(prepared.tx_hash)).decode()}
            ),
            label="transaction commit",
        )
        assert int(receipt["tx_result"].get("code", 0)) == 0, receipt["tx_result"]
        return prepared.tx_hash

    def start(self, index):
        home = self.output / f"node-{index}"
        for kind, command in [
            ("app", [sys.executable, str(Path(__file__).resolve()), "--node-home", str(home)]),
            ("comet", [self.binary, "start", "--home", str(home)]),
        ]:
            log = (self.output / f"node-{index}-{kind}.log").open("ab")
            self.logs.append(log)
            self.processes[index, kind] = subprocess.Popen(
                command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
            )

    def stop(self, index, *, crash=False):
        for kind in ("comet", "app"):
            process = self.processes.pop((index, kind), None)
            if process is None or process.poll() is not None:
                continue
            os.killpg(process.pid, signal.SIGKILL if crash else signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)

    def close(self):
        for index in range(NODES):
            self.stop(index)
        for log in self.logs:
            log.close()

    def height(self, node=0):
        return int(self.rpc("status", node=node)["sync_info"]["latest_block_height"])

    def convergence(self, nodes=range(NODES)):
        nodes = list(nodes)
        heights = [self.height(n) for n in nodes]
        common = min(heights) - 1
        assert common > 0, heights
        hashes = [
            self.rpc("block", {"height": str(common)}, n)["block"]["header"]["app_hash"]
            for n in nodes
        ]
        assert len(set(hashes)) == 1, hashes
        records = [self.query("_accounts.records:" + self.sender, n) for n in nodes]
        assert all(r == records[0] for r in records), "registry divergence"
        for key in self.invariants:
            values = [self.query(key, n) for n in nodes]
            assert all(v == values[0] for v in values), (key, values)
        return {
            "height": common,
            "app_hash": hashes[0],
            "epoch": records[0]["epoch"] if records[0] else None,
        }

    def balances(self):
        return {key: self.query(key) for key in self.invariants}

    def js_probe(self, action, **options):
        data = dict(
            action=action,
            rpcUrl=f"http://127.0.0.1:{self.base_port + 1}",
            chainId=CHAIN,
            sender=self.sender,
            **options,
        )
        result = subprocess.run(
            ["node", str(ROOT / "scripts/account-rehearsal-js.mjs")],
            input=json.dumps(data),
            text=True,
            capture_output=True,
            timeout=60,
        )
        if result.returncode:
            raise RuntimeError("JavaScript live SDK probe failed: " + result.stderr[-2000:])
        assert json.loads(result.stdout)["passed"]

    def draft(self, epoch):
        from xian_py.hybrid import AccountTransitionDraft

        draft = AccountTransitionDraft(CHAIN, self.sender, epoch)
        for role in ("active", "recovery"):
            path = self.output / f"epoch-{epoch}-{role}.json"
            with path.open("x") as handle:
                handle.write(draft.export_backup(role, self.password))
                handle.flush()
                os.fsync(handle.fileno())
            draft.verify_backup(role, path.read_text(), self.password)
        assert draft.ready
        return draft

    def setup(self):
        from xian.genesis_builder import build_local_network_genesis
        from xian.node_setup import (
            MetricsOptions,
            NodeConfigOptions,
            SimulationOptions,
            build_node_key,
            generate_validator_material,
            materialize_cometbft_home,
            render_node_configs,
        )
        from xian.state_root import StateRootCache
        from xian_py.wallet import Wallet

        for i in range(NODES):
            for port in (self.base_port + i * 10, self.base_port + i * 10 + 1):
                with socket.socket() as sock:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    sock.bind(("127.0.0.1", port))
        self.legacy = Wallet()
        self.sender = self.legacy.public_key
        self.recipient = Wallet().public_key
        founder = Wallet()
        material = [generate_validator_material() for _ in range(NODES)]
        validators = [
            {
                "account_public_key": v["validator_public_key_hex"],
                "priv_validator_key": v["priv_validator_key"],
                "name": f"rehearsal-{i}",
                "power": 10,
            }
            for i, v in enumerate(material)
        ]
        genesis = build_local_network_genesis(
            chain_id=CHAIN,
            founder_private_key=founder.private_key,
            validators=validators,
            contracts_dir=ROOT.parent / "xian-configs/contracts",
        )
        entries = {e["key"]: e["value"] for e in genesis["abci_genesis"]["genesis"]}
        assert entries["_accounts.policy"]["enrollment_open"] is False, (
            "default enrollment unexpectedly enabled"
        )
        entries.update(
            {
                "_accounts.policy": {
                    "version": 1,
                    "enrollment_open": self.enrollment_open,
                    "transition_chi": 1000,
                },
                f"currency.balances:{self.sender}": 10000,
                f"con_rehearsal.balances:{self.sender}": 123,
                f"con_rehearsal.allowances:{self.sender}:spender": 17,
                "con_rehearsal.owner": self.sender,
            }
        )
        self.invariants = [
            f"currency.balances:{self.sender}",
            f"con_rehearsal.balances:{self.sender}",
            f"con_rehearsal.allowances:{self.sender}:spender",
            "con_rehearsal.owner",
        ]
        self.configure_fixture_entries(entries)
        genesis["abci_genesis"]["genesis"] = [
            {"key": k, "value": v} for k, v in sorted(entries.items())
        ]
        genesis["abci_genesis"]["hash"] = StateRootCache(entries.items()).root_hash.hex()
        genesis["abci_genesis"]["origin"] = {"sender": "", "signature": ""}
        genesis["app_hash"] = genesis["abci_genesis"]["hash"]
        keys = [build_node_key() for _ in range(NODES)]
        for i in range(NODES):
            home = self.output / f"node-{i}"
            # macOS Unix socket paths must remain below its short pathname limit.
            sock = f"/tmp/xian-rehearsal-{os.getpid()}-{i}.sock"
            config = render_node_configs(
                options=NodeConfigOptions(
                    moniker=f"account-rehearsal-{i}",
                    proxy_app="unix://" + sock,
                    p2p_persistent_peers=tuple(
                        f"{keys[j]['node_id'].lower()}@127.0.0.1:{self.base_port + j * 10}"
                        for j in range(NODES)
                        if j != i
                    ),
                    block_policy_mode="periodic",
                    block_policy_interval="1s",
                    tx_fee_mode="free_metered",
                    metrics=MetricsOptions(enabled=False),
                    simulation=SimulationOptions(enabled=False),
                    prometheus=False,
                )
            )
            comet = config["cometbft"]
            comet["rpc"]["laddr"] = f"tcp://127.0.0.1:{self.base_port + i * 10 + 1}"
            comet["p2p"].update(
                laddr=f"tcp://127.0.0.1:{self.base_port + i * 10}",
                allow_duplicate_ip=True,
                addr_book_strict=False,
            )
            comet["consensus"].update(timeout_commit="500ms", timeout_propose="1s")
            materialize_cometbft_home(
                home=home,
                config=comet,
                xian_config=config["xian"],
                genesis=genesis,
                priv_validator_key=material[i]["priv_validator_key"],
                node_key=keys[i],
            )
        self.report["sender"] = self.sender
        self.report["sources"] = {
            repo: source_identity(repo)
            for repo in (
                "xian-abci",
                "xian-contracting",
                "xian-py",
                "xian-js",
                "xian-configs",
                "xian-stack",
            )
        }
        self.mark(
            "isolated-genesis",
            default_enrollment_open=False,
            fixture_enrollment_open=self.enrollment_open,
            fee_mode="free_metered",
        )

    def configure_fixture_entries(self, entries):
        """Specialized disposable rehearsals may seed extra state before hashing."""

    def run(self):
        from xian_py.hybrid import HybridWallet, decrypt_backup

        self.setup()
        for i in range(NODES):
            self.start(i)
        wait_for(lambda: all(self.height(i) >= 3 for i in range(NODES)), label="five validators")
        assert int(self.rpc("validators")["total"]) == NODES
        from xian_py import Xian

        source = """balances = Hash(default_value=0)
allowances = Hash(default_value=0)
owner = Variable()
@construct
def seed():
    owner.set(ctx.caller)
    balances[ctx.caller] = 123
    allowances[ctx.caller, 'spender'] = 17
@export
def transfer(to: str, amount: int):
    assert ctx.caller == owner.get(), 'original owner required'
    assert balances[ctx.caller] >= amount
    balances[ctx.caller] -= amount
    balances[to] += amount
"""
        with Xian(
            f"http://127.0.0.1:{self.base_port + 1}", wallet=self.legacy, chain_id=CHAIN
        ) as client:
            result = client.submit_contract("con_rehearsal", source, chi=1000000, wait_for_tx=True)
            assert result.accepted and result.finalized, result
        initial = self.balances()
        assert initial["con_rehearsal.owner"] == self.sender, (
            "owner identity was not decoded exactly"
        )
        self.mark("five-validator-health", **self.convergence())
        self.js_probe("absent")
        self.mark("javascript-absent-registry-wire-format")
        draft = self.draft(0)
        tx = draft.sign(
            self.payload(self.sender, "_accounts", "enroll", {"record": draft.active["record"]}),
            self.legacy.private_key,
        )
        # Persist exact retry bytes before submission, just as a custody adapter must.
        durable_json(self.output / "pending-enrollment.json", tx)
        self.broadcast(tx)
        enrollment_record = draft.active["record"]
        wait_for(
            lambda: all(
                self.query("_accounts.records:" + self.sender, i) == enrollment_record
                for i in range(NODES)
            ),
            label="enrollment convergence",
        )
        assert self.balances() == initial
        self.mark("enrollment-preserves-balances-and-permissions", **self.convergence())
        old = {
            "payload": self.payload(
                self.sender, "currency", "transfer", {"to": self.recipient, "amount": 1}
            )
        }
        from xian_py.transaction import canonical_json

        old["metadata"] = {"signature": self.legacy.sign_msg(canonical_json(old["payload"]))}
        for i in range(NODES):
            self.broadcast(old, expect=1, node=i)
        self.mark("legacy-authority-rejected-on-all-validators")
        assert self.query("permit_authorizer.__source__") is None
        assert self.query(f"currency.approvals:{self.sender}:{self.recipient}") in (0, None)
        assert self.balances() == initial
        self.mark("legacy-permit-contract-absent")
        self.stop(4, crash=True)
        active = HybridWallet(
            decrypt_backup((self.output / "epoch-0-active.json").read_text(), self.password)
        )
        self.broadcast(
            active.sign_transaction(
                self.payload(
                    self.sender, "currency", "transfer", {"to": self.recipient, "amount": 7}
                )
            )
        )
        self.broadcast(
            active.sign_transaction(
                self.payload(
                    self.sender, "con_rehearsal", "transfer", {"to": self.recipient, "amount": 1}
                )
            )
        )
        expected = dict(initial)
        expected[f"con_rehearsal.balances:{self.sender}"] = 122
        expected[f"currency.balances:{self.sender}"] = 9993
        assert self.balances() == expected
        self.start(4)
        wait_for(
            lambda: self.query(f"currency.balances:{self.sender}", 4) == 9993,
            label="crashed validator catch-up",
        )
        self.mark("single-validator-crash-spend-and-catchup", **self.convergence())
        # Lose active credentials; restore only the offline recovery file.
        del active, draft, self.legacy
        recovery = decrypt_backup(
            (self.output / "epoch-0-recovery.json").read_text(), self.password
        )
        next_draft = self.draft(1)
        tx = next_draft.sign(
            self.payload(
                self.sender, "_accounts", "recover", {"record": next_draft.active["record"]}
            ),
            recovery,
        )
        durable_json(self.output / "pending-recovery.json", tx)
        self.stop(3, crash=True)
        self.stop(4, crash=True)
        time.sleep(2)
        height = self.height()
        tx_hash = self.broadcast(tx, wait=False)
        time.sleep(3)
        assert self.height() == height, "committed without quorum"
        assert self.query("_accounts.records:" + self.sender)["epoch"] == 0
        assert self.balances() == expected
        self.mark("quorum-loss-keeps-old-authority-and-balances", height=height)
        # Restart the submitting validator too: discard its in-memory mempool,
        # then use persisted exact bytes and verified backups after quorum returns.
        self.stop(0, crash=True)
        for i in (0, 3, 4):
            self.start(i)
        wait_for(lambda: all(self.height(i) > height for i in range(NODES)), label="quorum restart")
        if self.query("_accounts.records:" + self.sender)["epoch"] == 0:
            self.broadcast(json.loads((self.output / "pending-recovery.json").read_text()))
        wait_for(
            lambda: all(
                self.query("_accounts.records:" + self.sender, i)["epoch"] == 1
                for i in range(NODES)
            ),
            label="recovery convergence",
        )
        assert self.balances() == expected
        self.mark(
            "recovery-after-quorum-loss-and-client-retry", transaction=tx_hash, **self.convergence()
        )
        stale = HybridWallet(
            decrypt_backup((self.output / "epoch-0-active.json").read_text(), self.password)
        )
        self.broadcast(
            stale.sign_transaction(
                self.payload(
                    self.sender, "currency", "transfer", {"to": self.recipient, "amount": 100}
                )
            ),
            expect=1,
        )
        stale_draft = self.draft(2)
        from xian_py.hybrid import SCHEME, TX_FORMAT, _proof

        stale_payload = self.payload(
            self.sender, "_accounts", "recover", {"record": stale_draft.active["record"]}
        )
        # Bypass the SDK's early stale-epoch rejection to exercise every validator.
        stale_tx = {
            "payload": stale_payload,
            "metadata": {
                "tx_format": TX_FORMAT,
                "auth": {
                    "scheme": SCHEME,
                    "signature": _proof(recovery, stale_payload, "authority"),
                    "active_proof": _proof(stale_draft.active, stale_payload, "new_active"),
                    "recovery_proof": _proof(stale_draft.recovery, stale_payload, "new_recovery"),
                },
            },
        }
        for i in range(NODES):
            self.broadcast(stale_tx, expect=1, node=i)
        self.mark("obsolete-active-and-recovery-backups-rejected")
        for i in range(NODES):
            self.stop(i, crash=True)
        for i in range(NODES):
            self.start(i)
        wait_for(
            lambda: all(self.height(i) > height + 2 for i in range(NODES)),
            label="full network restart",
        )
        restored = HybridWallet(
            decrypt_backup((self.output / "epoch-1-active.json").read_text(), self.password)
        )
        self.broadcast(
            restored.sign_transaction(
                self.payload(
                    self.sender, "currency", "transfer", {"to": self.recipient, "amount": 3}
                )
            )
        )
        expected[f"currency.balances:{self.sender}"] = 9990
        wait_for(
            lambda: all(
                self.query(f"currency.balances:{self.sender}", i) == 9990 for i in range(NODES)
            ),
            label="final balances",
        )
        assert self.balances() == expected
        self.mark(
            "full-network-restart-and-restored-spending", balances=expected, **self.convergence()
        )
        self.js_probe(
            "spend",
            backup=(self.output / "epoch-1-active.json").read_text(),
            password=self.password,
            payload=self.payload(
                self.sender, "currency", "transfer", {"to": self.recipient, "amount": 2}
            ),
        )
        self.js_probe(
            "spend",
            backup=(self.output / "epoch-1-active.json").read_text(),
            password=self.password,
            payload=self.payload(
                self.sender, "con_rehearsal", "transfer", {"to": self.recipient, "amount": 2}
            ),
        )
        expected[f"con_rehearsal.balances:{self.sender}"] = 120
        expected[f"currency.balances:{self.sender}"] = 9988
        wait_for(
            lambda: all(
                self.query(f"currency.balances:{self.sender}", i) == 9988 for i in range(NODES)
            ),
            label="JS spend convergence",
        )
        assert self.balances() == expected
        assert self.query(f"currency.balances:{self.recipient}") == 12
        assert self.query(f"con_rehearsal.balances:{self.recipient}") == 3
        self.mark(
            "javascript-restores-python-backup-and-spends", balances=expected, **self.convergence()
        )
        self.report["passed"] = True
        self.save_report()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node-home", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cometbft", default="cometbft")
    parser.add_argument("--base-port", type=int, default=38650)
    args = parser.parse_args()
    if args.node_home:
        node_main(args.node_home)
        return
    binary = shutil.which(args.cometbft)
    if not binary:
        parser.error("CometBFT binary is required")
    os.umask(0o077)
    output = (
        args.output or ROOT / ".artifacts/account-migration" / time.strftime("%Y%m%d-%H%M%S")
    ).resolve()
    runner = Rehearsal(output, binary, args.base_port)
    try:
        runner.run()
    except BaseException as exc:
        runner.report.update(passed=False, error=str(exc))
        runner.save_report()
        raise
    finally:
        runner.close()
    print(f"Report: {output / 'report.json'}", flush=True)


if __name__ == "__main__":
    main()
