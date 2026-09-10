"""Seeded model-based asset conservation, fee accounting, and rollback checks."""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from decimal import Decimal

from localnet_chain_checks import agreement, command, deploy, require, wallet

LEDGER_SOURCE = """balances = Hash(default_value=0)
staked = Hash(default_value=0)
supply = Variable()
owner = Variable()

@construct
def seed():
    owner.set(ctx.caller)
    supply.set(0)

@export
def mint(account: str, amount: int):
    assert ctx.caller == owner.get(), "only owner"
    assert amount > 0, "positive amount required"
    balances[account] += amount
    supply.set(supply.get() + amount)

@export
def move(account: str, amount: int):
    assert amount > 0 and balances[ctx.caller] >= amount, "insufficient balance"
    balances[ctx.caller] -= amount
    balances[account] += amount

@export
def stake(amount: int):
    assert amount > 0 and balances[ctx.caller] >= amount, "insufficient balance"
    balances[ctx.caller] -= amount
    staked[ctx.caller] += amount

@export
def unstake(amount: int):
    assert amount > 0 and staked[ctx.caller] >= amount, "insufficient stake"
    staked[ctx.caller] -= amount
    balances[ctx.caller] += amount

@export
def burn(amount: int):
    assert amount > 0 and balances[ctx.caller] >= amount, "insufficient balance"
    balances[ctx.caller] -= amount
    supply.set(supply.get() - amount)

@export
def rollback(account: str, amount: int):
    balances[ctx.caller] -= amount
    balances[account] += amount
    supply.set(supply.get() + amount)
    assert False, "intentional rollback"

@export
def report(accounts: list):
    return {'balances': [balances[a] for a in accounts],
            'staked': [staked[a] for a in accounts], 'supply': supply.get()}
"""


@dataclass
class LedgerModel:
    balances: list[int] = field(default_factory=lambda: [1000] * 4)
    staked: list[int] = field(default_factory=lambda: [0] * 4)
    supply: int = 4000

    def apply(self, action, sender, receiver, amount):
        if amount <= 0:
            return False
        if action == "rollback":
            return False
        if action == "mint":
            self.balances[receiver] += amount
            self.supply += amount
            return True
        available = self.staked if action == "unstake" else self.balances
        if amount <= 0 or available[sender] < amount:
            return False
        available[sender] -= amount
        if action == "move":
            self.balances[receiver] += amount
        elif action == "stake":
            self.staked[sender] += amount
        elif action == "unstake":
            self.balances[sender] += amount
        elif action == "burn":
            self.supply -= amount
        else:
            raise ValueError(action)
        return True

    def report(self):
        assert sum(self.balances) + sum(self.staked) == self.supply
        assert min(self.balances + self.staked) >= 0
        return {"balances": self.balances[:], "staked": self.staked[:], "supply": self.supply}


async def native_balances(runner, actors):
    script = """import json,sys
from decimal import Decimal
from pathlib import Path
from contracting.storage.driver import Driver
from xian.utils.block import get_committed_latest_block
p=Path('/root/.cometbft/xian')
d=Driver(storage_home=p)
s=d._store.items()
actors=json.loads(sys.argv[1])
values=[Decimal(str(v)) for k,v in s.items() if k.startswith('currency.balances:')]
print(json.dumps({'total':str(sum(values,Decimal(0))), 'balance_count':len(values),
 'balances':{a:str(s.get('currency.balances:'+a,0)) for a in actors},
 'chi_cost':str(s['chi_cost.S:value']),
 'burn_ratio':str(s['rewards.S:value'][1]),
 'height':get_committed_latest_block(d)['height']}))
"""
    return json.loads(
        await command(
            "docker",
            "exec",
            "-i",
            runner.nodes[0].abci_container,
            "python",
            "-",
            json.dumps(actors),
            input=script,
        )
    )


async def accounting_phase(runner, session):
    name = await deploy(runner, session, "ledger", LEDGER_SOURCE)
    actors = [wallet(runner, f"ledger-{i}") for i in range(4)]
    accounts = [w.public_key for w in actors]
    await runner.fund_wallets(session, actors, amount=1000)
    async with runner.client(runner.founder_wallet, 0, session) as client:
        for account in accounts:
            result = await client.send_tx(
                name, "mint", {"account": account, "amount": 1000}, chi=15000, wait_for_tx=True
            )
            require(result.receipt.success, "Initial ledger allocation failed")
    model = LedgerModel()
    rng = random.Random(f"{runner.seed}:ledger")
    rounds = getattr(runner.args, "invariant_rounds", 48)
    require(rounds >= 12, "Accounting workload needs at least 12 rounds")
    operations = []
    native_start = await native_balances(runner, accounts)
    expected_native_burn = Decimal(0)
    for index in range(rounds):
        # A fixed prefix guarantees every operation, including failures; the
        # remaining sequence explores reproducible combinations and amounts.
        action = ("move", "stake", "unstake", "burn", "rollback", "mint")[index % 6]
        sender, receiver = rng.randrange(4), rng.randrange(4)
        amount = rng.randint(1, 70)
        if index < 6:
            sender, receiver, amount = 0, 1, 10
        if index % 11 == 8:
            amount = 0
        elif index % 11 == 9:
            amount = -1
        elif index % 11 == 10:
            amount = 100000
        kwargs = {"amount": amount}
        if action in {"move", "rollback", "mint"}:
            kwargs["account"] = accounts[receiver]
        signer = runner.founder_wallet if action == "mint" else actors[sender]
        before = await native_balances(runner, [*accounts, runner.founder_wallet.public_key])
        expected_success = model.apply(action, sender, receiver, amount)
        async with runner.client(signer, index % len(runner.nodes), session) as client:
            result = await client.send_tx(name, action, kwargs, chi=15000, wait_for_tx=True)
        require(result.accepted and result.finalized, "Accounting transaction did not finalize")
        require(result.receipt.success == expected_success, f"Ledger outcome drift at step {index}")
        await agreement(session, runner.nodes, window=1)
        async with runner.client(actors[0], 0, session) as client:
            actual = await client.call(name, "report", {"accounts": accounts})
        require(actual == model.report(), f"Ledger accounting/rollback drift at step {index}")
        execution = result.receipt.execution
        fee = Decimal(str(execution["chi_used"])) / Decimal(before["chi_cost"])
        after = await native_balances(runner, [*accounts, runner.founder_wallet.public_key])
        # Failed calls pay execution fees but produce no fee-reward payouts.
        expected_burn = fee * Decimal(before["burn_ratio"]) if expected_success else fee
        tolerance = Decimal("0.00000001") * (before["balance_count"] + 1)
        actual_burn = Decimal(before["total"]) - Decimal(after["total"])
        require(
            abs(actual_burn - expected_burn) <= tolerance,
            f"Native currency conservation drift at step {index}: {actual_burn} vs {expected_burn}",
        )
        if action != "mint":
            spent = Decimal(before["balances"][signer.public_key]) - Decimal(
                after["balances"][signer.public_key]
            )
            require(
                abs(spent - fee) <= Decimal("0.00000001"),
                f"Sender fee charged incorrectly at step {index}",
            )
        expected_native_burn += expected_burn
        operations.append(
            {
                "step": index,
                "action": action,
                "sender": sender,
                "receiver": receiver,
                "amount": amount,
                "success": expected_success,
                "fee": str(fee),
                "native_burn": str(actual_burn),
                "tx_hash": result.receipt.tx_hash,
            }
        )
        (runner.output_dir / "accounting-trace.json").write_text(
            json.dumps({"seed": f"{runner.seed}:ledger", "operations": operations}, indent=2) + "\n"
        )
        if index == rounds // 2:
            await runner.restart_localnet_and_wait_ready(session)
    final = await native_balances(runner, accounts)
    require(
        abs(Decimal(native_start["total"]) - Decimal(final["total"]) - expected_native_burn)
        <= Decimal("0.00000001") * (final["balance_count"] + 1) * rounds,
        "Cumulative native currency conservation drift",
    )
    return {
        "seed": f"{runner.seed}:ledger",
        "rounds": rounds,
        "operations": operations,
        "asset_model": model.report(),
        "native_start": native_start,
        "native_final": final,
        "expected_native_burn": str(expected_native_burn),
        "consensus": await agreement(session, runner.nodes),
    }
