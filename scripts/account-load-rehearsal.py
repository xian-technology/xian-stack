#!/usr/bin/env python3
"""Bounded sustained load on five NEW loopback validators; enrollment stays closed.

Uses current sibling sources and disposable funded/registered fixtures. No RPC
endpoint option exists: this harness cannot target an existing/public network.
It never writes final genesis, .localnet, or shipping configuration.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import copy
import importlib.util
import os
import shutil
import statistics
import subprocess
import time
from collections import Counter
from pathlib import Path

import aiohttp

SPEC = importlib.util.spec_from_file_location(
    "account_rehearsal", Path(__file__).with_name("account-migration-rehearsal.py")
)
base = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(base)


def summary(values):
    ordered = sorted(values)
    return (
        {}
        if not ordered
        else {
            "count": len(ordered),
            "p50_ms": statistics.median(ordered),
            "p95_ms": ordered[(95 * len(ordered) - 1) // 100],
            "max_ms": ordered[-1],
        }
    )


def expected_balances(initial, receipts, recipient, transitions=()):
    """Only successful, distinct committed transfers contribute to the ledger."""
    result = dict(initial)
    seen = set()
    nonces = {}
    skipped = {(item["address"], item["nonce"]) for item in transitions}
    for tx in receipts:
        if tx["hash"] in seen:
            raise AssertionError("duplicate committed transaction in ledger")
        seen.add(tx["hash"])
        sender = tx["sender"]
        expected = nonces.get(sender, -1) + 1
        if (sender, expected) in skipped:
            expected += 1
        if tx["nonce"] != expected:
            raise AssertionError("non-contiguous committed transfer nonce")
        nonces[sender] = tx["nonce"]
        assert tx["amount"] == 1 and tx["to"] == recipient
        result[sender] -= 1
        result[recipient] += 1
    assert sum(result.values()) == sum(initial.values())
    assert all(value >= 0 for value in result.values())
    return result


class LoadRehearsal(base.Rehearsal):
    def __init__(self, output, binary, base_port, *, baseline, attack, recovery, invalid_rps):
        super().__init__(output, binary, base_port, enrollment_open=False)
        self.durations = (baseline, attack, recovery)
        self.invalid_rps = invalid_rps
        self.accounts = []
        self.receipts = []
        self.live = set()
        self.phase = None
        self.report.update(
            kind="sustained-account-load",
            enrollment_open=False,
            launch_approved=False,
            final_genesis_created=False,
            fee_mode="free_metered",
            phases=[],
            checkpoints=[],
            resource_samples=[],
        )

    def new_draft(self, sender, epoch):
        from xian_py.account_master import MasterAccountTransitionDraft, import_account_master

        draft = MasterAccountTransitionDraft(base.CHAIN, sender, epoch)
        path = self.output / f"{sender}-epoch-{epoch}-master.json"
        with path.open("x") as handle:
            handle.write(draft.export_master_backup(self.password))
            handle.flush()
            os.fsync(handle.fileno())
        draft.verify_master_backup(path.read_text(), self.password)
        assert import_account_master(path.read_text(), self.password)["sender"] == sender
        assert draft.ready
        return draft

    def configure_fixture_entries(self, entries):
        from xian_py.hybrid import HybridWallet
        from xian_py.wallet import Wallet

        self.accounts.append(
            {
                "address": self.sender,
                "signer": self.legacy,
                "legacy": self.legacy,
                "record": None,
                "nonce": 0,
            }
        )
        for _ in range(4):
            old = Wallet()
            draft = self.new_draft(old.public_key, 0)
            self.accounts.append(
                {
                    "address": old.public_key,
                    "signer": HybridWallet(draft.active),
                    "legacy": old,
                    "record": draft.active["record"],
                    "nonce": 0,
                    "draft": draft,
                }
            )
            entries["currency.balances:" + old.public_key] = 10000
            entries["_accounts.records:" + old.public_key] = draft.active["record"]
        # These fixtures replace funding from the development founder; no mint.
        donor = max(
            (
                k
                for k in entries
                if k.startswith("currency.balances:")
                and k.split(":")[1] not in {a["address"] for a in self.accounts}
            ),
            key=lambda k: entries[k],
        )
        entries[donor] -= 50000  # includes the base fixture's legacy allocation
        assert entries[donor] >= 0
        assert entries["_accounts.policy"]["enrollment_open"] is False
        self.initial = {a["address"]: 10000 for a in self.accounts} | {self.recipient: 0}
        self.report["accounts"] = [
            {"address": a["address"], "authorization": "hybrid" if a["record"] else "ed25519"}
            for a in self.accounts
        ]
        self.report["initial_balances"] = self.initial

    def start(self, index):
        super().start(index)
        self.live.add(index)

    def stop(self, index, *, crash=False):
        self.live.discard(index)
        super().stop(index, crash=crash)

    def signed(self, account, *, nonce=None):
        from xian_py.transaction import canonical_json

        payload = dict(
            sender=account["address"],
            chain_id=base.CHAIN,
            nonce=account["nonce"] if nonce is None else nonce,
            chi_supplied=100000,
            contract="currency",
            function="transfer",
            kwargs={"to": self.recipient, "amount": 1},
        )
        if account["record"]:
            return account["signer"].sign_transaction(payload)
        return {
            "payload": payload,
            "metadata": {"signature": account["signer"].sign_msg(canonical_json(payload))},
        }

    def corpus(self):
        from xian.utils.encoding import canonical_json_text, encode_transaction_bytes
        from xian_py.transaction import canonical_json, prepare_transaction

        result = []
        account = self.accounts[1]
        for i in range(32):
            good = self.signed(account, nonce=1000000 + i)
            result.append(("valid_proof_wrong_nonce", prepare_transaction(good).submitted_bytes))
            for kind in ("bad_ed", "bad_pq", "wrong_chain", "old_key", "closed_enrollment"):
                tx = copy.deepcopy(good)
                if kind in ("bad_ed", "bad_pq"):
                    sig = bytearray.fromhex(tx["metadata"]["auth"]["signature"])
                    sig[28 if kind == "bad_ed" else -1] ^= 1
                    tx["metadata"]["auth"]["signature"] = sig.hex()
                elif kind == "wrong_chain":
                    tx["payload"]["chain_id"] = "wrong-local-chain"
                elif kind == "old_key":
                    tx["metadata"] = {
                        "signature": account["legacy"].sign_msg(canonical_json(tx["payload"]))
                    }
                else:
                    tx["payload"].update(
                        sender=self.sender,
                        contract="_accounts",
                        function="enroll",
                        kwargs={"record": account["record"]},
                    )
                    tx["metadata"]["auth"].update(active_proof="", recovery_proof="")
                result.append((kind, encode_transaction_bytes(canonical_json_text(tx))))
        result.append(("deep_json", encode_transaction_bytes("[" * 2000 + "]" * 2000)))
        oversized = self.signed(account, nonce=1000001)
        oversized["payload"]["kwargs"]["padding"] = [None] * 400000
        result.append(
            ("oversized_flat_json", encode_transaction_bytes(canonical_json_text(oversized)))
        )
        result.append(("malformed_hex", b"not-hex"))
        return result

    async def arpc(self, method, params=None, node=0):
        async with self.session.post(
            f"http://127.0.0.1:{self.base_port + node * 10 + 1}",
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        ) as response:
            response.raise_for_status()
            value = await response.json()
        if "error" in value:
            if value["error"].get("data") == "endpoint is closed while node is catching up":
                raise OSError("validator is catching up")
            raise ValueError(value["error"])
        return value["result"]

    async def committed(self, prepared, node):
        try:
            result = await self.arpc(
                "tx", {"hash": base64.b64encode(bytes.fromhex(prepared.tx_hash)).decode()}, node
            )
        except ValueError as exc:
            if "not found" in str(exc):
                return None
            raise
        assert int(result["tx_result"].get("code", 0)) == 0, result
        return result

    async def transfer_worker(self, account, end, stats):
        from xian_py.transaction import prepare_transaction

        while time.monotonic() < end:
            tx = self.signed(account)
            prepared = prepare_transaction(tx)
            started = time.monotonic()
            deadline = started + 120
            last_send = 0
            receipt = None
            while time.monotonic() < deadline:
                if not self.live:
                    await asyncio.sleep(0.1)
                    continue
                node = sorted(self.live)[account["nonce"] % len(self.live)]
                try:
                    if time.monotonic() - last_send > 3:
                        last_send = time.monotonic()
                        try:
                            result = await self.arpc(
                                "broadcast_tx_sync",
                                {"tx": base64.b64encode(prepared.submitted_bytes).decode()},
                                node,
                            )
                            stats["broadcast_ms"].append((time.monotonic() - last_send) * 1000)
                            if int(result.get("code", 0)):
                                # A response may race commit/recheck. Only an exact
                                # successful receipt can resolve this transaction.
                                stats["valid_checktx_rejections"] += 1
                        except ValueError as exc:
                            if "cache" not in str(exc):
                                raise
                            stats["duplicate_cache_hits"] += 1
                    receipt = await self.committed(prepared, node)
                    if receipt:
                        break
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                    stats["valid_transport_errors"] += 1
                await asyncio.sleep(0.15)
            assert receipt is not None, f"unresolved transfer {prepared.tx_hash}"
            elapsed = (time.monotonic() - started) * 1000
            stats["commit_ms"].append(elapsed)
            self.receipts.append(
                {
                    "hash": prepared.tx_hash,
                    "sender": account["address"],
                    "nonce": account["nonce"],
                    "amount": 1,
                    "to": self.recipient,
                    "height": int(receipt["height"]),
                    "phase": stats["name"],
                }
            )
            account["nonce"] += 1
            stats["committed"] += 1

    async def invalid_worker(self, index, count, end, stats, corpus):
        interval = count / self.invalid_rps
        sequence = index
        while time.monotonic() < end:
            started = time.monotonic()
            if self.live:
                kind, raw = corpus[sequence % len(corpus)]
                node = sorted(self.live)[sequence % len(self.live)]
                stats["invalid_attempts"] += 1
                try:
                    result = await self.arpc(
                        "broadcast_tx_sync", {"tx": base64.b64encode(raw).decode()}, node
                    )
                    assert int(result.get("code", 0)) != 0, ("invalid accepted", kind, result)
                    stats["invalid_rejected"][kind] += 1
                    stats["invalid_ms"].append((time.monotonic() - started) * 1000)
                except ValueError as exc:
                    if "cache" not in str(exc):
                        raise
                    stats["invalid_cache_hits"] += 1
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                    stats["invalid_transport_errors"] += 1
                sequence += count
            await asyncio.sleep(max(0, interval - (time.monotonic() - started)))

    async def monitor(self, end, stats):
        while time.monotonic() < end:
            sample = {
                "elapsed_s": round(time.monotonic() - stats["started"], 2),
                "phase": stats["name"],
                "nodes": {},
            }
            for node in sorted(self.live):
                try:
                    status = await self.arpc("status", node=node)
                    sample["nodes"][str(node)] = int(status["sync_info"]["latest_block_height"])
                except (ValueError, aiohttp.ClientError, asyncio.TimeoutError, OSError):
                    sample["nodes"][str(node)] = None
            pids = [str(p.pid) for p in self.processes.values() if p.poll() is None]
            if pids:
                usage = await asyncio.to_thread(
                    subprocess.check_output,
                    ["ps", "-o", "pid=,pcpu=,rss=", "-p", ",".join(pids)],
                    text=True,
                )
                sample["process_usage"] = [
                    dict(pid=int(p), cpu_percent=float(c), rss_kib=int(r))
                    for p, c, r in (line.split() for line in usage.splitlines())
                ]
            sample["log_bytes"] = sum(p.stat().st_size for p in self.output.glob("*.log"))
            self.report["resource_samples"].append(sample)
            self.save_report()
            print(
                f"[load] {stats['name']} elapsed={sample['elapsed_s']}s "
                f"committed={stats['committed']} "
                f"invalid={sum(stats['invalid_rejected'].values())} heights={sample['nodes']}",
                flush=True,
            )
            await asyncio.sleep(min(10, max(0, end - time.monotonic())))

    async def faults(self, duration):
        await asyncio.sleep(duration * 0.2)
        await asyncio.to_thread(self.stop, 4, crash=True)
        self.mark("validator-4-killed-during-load")
        await asyncio.sleep(duration * 0.2)
        self.start(4)
        self.mark("validator-4-restarted-during-load")
        await asyncio.sleep(duration * 0.25)
        for node in range(base.NODES):
            await asyncio.to_thread(self.stop, node, crash=True)
        self.mark("all-validators-killed-with-transfers-in-flight")
        await asyncio.sleep(3)
        for node in range(base.NODES):
            self.start(node)
        self.mark("all-validators-restarted-during-load")

    async def load_phase(self, name, duration, *, attack=False, faults=False):
        started = time.monotonic()
        stats = dict(
            name=name,
            started=started,
            requested_seconds=duration,
            committed=0,
            invalid_attempts=0,
            invalid_rejected=Counter(),
            invalid_cache_hits=0,
            invalid_transport_errors=0,
            valid_transport_errors=0,
            valid_checktx_rejections=0,
            duplicate_cache_hits=0,
            broadcast_ms=[],
            commit_ms=[],
            invalid_ms=[],
        )
        corpus = self.corpus() if attack else []
        started = stats["started"] = time.monotonic()
        end = started + duration
        tasks = [asyncio.create_task(self.transfer_worker(a, end, stats)) for a in self.accounts]
        tasks.append(asyncio.create_task(self.monitor(end, stats)))
        if attack:
            tasks.extend(
                asyncio.create_task(self.invalid_worker(i, 8, end, stats, corpus)) for i in range(8)
            )
        if faults:
            tasks.append(asyncio.create_task(self.faults(duration)))
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        elapsed = time.monotonic() - started
        stats.pop("started")
        for key in ("broadcast_ms", "commit_ms", "invalid_ms"):
            stats[key] = summary(stats[key])
        stats.update(
            elapsed_including_drain_s=elapsed,
            committed_tps=stats["committed"] / elapsed,
            achieved_invalid_rps=stats["invalid_attempts"] / duration,
        )
        stats["committed_by_account"] = dict(
            Counter(tx["sender"] for tx in self.receipts if tx["phase"] == name)
        )
        assert set(stats["committed_by_account"]) == {a["address"] for a in self.accounts}
        self.report["phases"].append(stats)
        await asyncio.to_thread(self.checkpoint, name)
        self.mark(
            "load-phase-complete",
            phase=name,
            committed=stats["committed"],
            invalid=stats["invalid_attempts"],
        )

    def checkpoint(self, name):
        expected = expected_balances(
            self.initial,
            self.receipts,
            self.recipient,
            [self.report["recovery"]] if "recovery" in self.report else [],
        )

        def matched():
            for node in range(base.NODES):
                if self.rpc("status", node=node)["sync_info"]["catching_up"]:
                    return False
                if self.query("_accounts.policy", node)["enrollment_open"] is not False:
                    raise AssertionError("enrollment opened")
                for address, amount in expected.items():
                    if (self.query("currency.balances:" + address, node) or 0) != amount:
                        return False
                for account in self.accounts:
                    if (
                        self.query("_accounts.records:" + account["address"], node)
                        != account["record"]
                    ):
                        return False
            return True

        base.wait_for(matched, timeout=90, label="all balances and authority records")
        assert all(
            int(self.rpc("validators", node=n)["total"]) == base.NODES for n in range(base.NODES)
        )
        evidence = self.convergence()
        self.report["checkpoints"].append(
            dict(name=name, balances=expected, transactions=len(self.receipts), **evidence)
        )
        base.durable_json(self.output / "committed-transfers.json", self.receipts)
        self.save_report()

    async def recover(self):
        from xian_py.account_master import read_account_credential
        from xian_py.hybrid import HybridWallet

        account = self.accounts[1]
        old_signer = account["signer"]
        authority = read_account_credential(
            (self.output / f"{account['address']}-epoch-0-master.json").read_text(),
            self.password,
            "recovery",
        )
        draft = self.new_draft(account["address"], 1)
        payload = self.payload(
            account["address"], "_accounts", "recover", {"record": draft.active["record"]}
        )
        tx = draft.sign(payload, authority)
        assert payload["nonce"] == account["nonce"]
        base.durable_json(self.output / "pending-recovery.json", tx)
        await asyncio.to_thread(self.broadcast, tx)
        account.update(
            record=draft.active["record"],
            signer=HybridWallet(
                read_account_credential(
                    (self.output / f"{account['address']}-epoch-1-master.json").read_text(),
                    self.password,
                    "active",
                )
            ),
            nonce=account["nonce"] + 1,
        )
        # Account transition consumes one nonce, but moves no money. Record it
        # separately so the transfer-ledger continuity check knows the gap.
        self.report["recovery"] = {
            "address": account["address"],
            "nonce": payload["nonce"],
            "epoch": 1,
        }
        stale = old_signer.sign_transaction(
            self.payload(
                account["address"], "currency", "transfer", {"to": self.recipient, "amount": 1}
            )
        )
        for node in range(base.NODES):
            await asyncio.to_thread(self.broadcast, stale, expect=1, node=node)
        self.mark("cold-recovery-and-stale-key-rejection", address=account["address"])

    async def run_async(self):
        self.setup()
        for node in range(base.NODES):
            self.start(node)
        await asyncio.to_thread(
            base.wait_for,
            lambda: all(self.height(n) >= 3 for n in range(base.NODES)),
            label="five validators",
        )
        assert int(self.rpc("validators")["total"]) == base.NODES
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10), connector=aiohttp.TCPConnector(limit=32)
        ) as session:
            self.session = session
            await asyncio.to_thread(self.checkpoint, "initial-closed-policy")
            await self.load_phase("baseline", self.durations[0])
            await self.load_phase(
                "mixed-load-and-restarts", self.durations[1], attack=True, faults=True
            )
            await self.recover()
            await self.load_phase("recovered-spending-under-attack", self.durations[2], attack=True)
            for node in range(base.NODES):
                await asyncio.to_thread(self.stop, node, crash=True)
            for node in range(base.NODES):
                self.start(node)
            await asyncio.to_thread(
                base.wait_for,
                lambda: all(self.height(n) > 3 for n in range(base.NODES)),
                label="final restart",
            )
            await asyncio.to_thread(self.checkpoint, "final-restart")
            await self.load_phase("spending-after-final-restart", 10)
        self.report["passed"] = True
        self.save_report()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cometbft", default="cometbft")
    parser.add_argument("--base-port", type=int, default=40650)
    parser.add_argument("--baseline-seconds", type=int, default=60)
    parser.add_argument("--attack-seconds", type=int, default=180)
    parser.add_argument("--recovery-seconds", type=int, default=120)
    parser.add_argument("--invalid-rps", type=int, default=80)
    args = parser.parse_args()
    for seconds in (args.baseline_seconds, args.attack_seconds, args.recovery_seconds):
        if not 10 <= seconds <= 3600:
            parser.error("phase durations must be between 10 and 3600 seconds")
    if not 1 <= args.invalid_rps <= 500 or not 1024 <= args.base_port <= 65000:
        parser.error("invalid rate or port range")
    binary = shutil.which(args.cometbft)
    if binary is None:
        parser.error("CometBFT binary required")
    os.umask(0o077)
    runner = LoadRehearsal(
        args.output.resolve(),
        binary,
        args.base_port,
        baseline=args.baseline_seconds,
        attack=args.attack_seconds,
        recovery=args.recovery_seconds,
        invalid_rps=args.invalid_rps,
    )
    try:
        asyncio.run(runner.run_async())
    except BaseException as exc:
        runner.report.update(passed=False, error=str(exc))
        runner.save_report()
        raise
    finally:
        runner.close()
    print(f"Report: {runner.output / 'report.json'}", flush=True)


if __name__ == "__main__":
    main()
