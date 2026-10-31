"""Authored contracts and explicit outcomes shared by native and five-node tests."""

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from xian_runtime_types.decimal import ContractingDecimal as Decimal

SOURCE_DIR = Path(__file__).resolve().parents[1] / "workloads/e2e/contract_suite"
ROLES = (
    "counter",
    "collections",
    "codec",
    "ledger",
    "escrow",
    "router",
    "inspector",
    "commitments",
)


@dataclass(frozen=True)
class Case:
    label: str
    contract: str
    function: str
    kwargs: dict = field(default_factory=dict)
    result: object = None
    state: dict = field(default_factory=dict)
    actor: str = "alice"
    error: str | None = None
    events: tuple = ()
    chi: int = 200_000


def sources(names):
    return {
        role: (SOURCE_DIR / f"{role}.py").read_text().replace("__LEDGER__", names["ledger"])
        for role in ROLES
    }


def constructors(accounts):
    return {
        "counter": {"owner": accounts["alice"], "initial": 2},
        "ledger": {"owner": accounts["alice"]},
    }


def event(contract, name, **data):
    return {"contract": contract, "event": name, "data": data}


def simple_cases(n, a):
    c, col, codec = n["counter"], n["collections"], n["codec"]
    containers = [["a", "b", "c"], ["a", "b", "c"], [[1, 2], [1, 2], [3]]]
    flow = [[0, 4, 16], {"0": 0, "1": 2, "2": 4}, 9, "yes"]
    text = ["alpha|beta", hashlib.sha256(b"alpha|beta").hexdigest(), "5a424344", "2:alpha"]
    return [
        Case(
            "counter-add",
            c,
            "add",
            {"delta": 3},
            5,
            {f"{c}.value": 5, f"{c}.history:recent:value": 5},
            events=(event(c, "Changed", value=5, caller=a["alice"]),),
        ),
        Case(
            "counter-second-actor",
            c,
            "add",
            {"delta": 4},
            9,
            {f"{c}.value": 9, f"{c}.history:recent:value": 9},
            actor="bob",
            events=(event(c, "Changed", value=9, caller=a["bob"]),),
        ),
        Case("counter-typed-argument", c, "add", {"delta": "bad"}, error="argument"),
        Case("counter-invalid-delta", c, "add", {"delta": 0}, error="positive delta"),
        Case("counter-write-event-rollback", c, "fail_after_write", error="counter deliberate"),
        Case("counter-unauthorized-reset", c, "reset", actor="bob", error="admin required"),
        Case(
            "counter-reset-key",
            c,
            "reset",
            result=0,
            state={f"{c}.value": 0, f"{c}.history:recent:value": None},
        ),
        Case(
            "counter-recovery",
            c,
            "add",
            {"delta": 1},
            1,
            {f"{c}.value": 1, f"{c}.history:recent:value": 1},
            events=(event(c, "Changed", value=1, caller=a["alice"]),),
        ),
        Case(
            "numeric-builtins",
            col,
            "numbers",
            result=[7, 12, 120],
            state={f"{col}.result": [7, 12, 120]},
        ),
        Case(
            "collection-methods",
            col,
            "containers",
            result=containers,
            state={f"{col}.result": containers},
        ),
        Case("flow-callables-iterators", col, "flow", result=flow, state={f"{col}.result": flow}),
        Case("out-of-chi-rollback", col, "flow", error="out of chi", chi=1),
        Case(
            "numeric-after-out-of-chi",
            col,
            "numbers",
            result=[7, 12, 120],
            state={f"{col}.result": [7, 12, 120]},
        ),
        Case(
            "text-bytes-hashing",
            codec,
            "transform",
            {"text": " Alpha,BETA "},
            text,
            {f"{codec}.result": text},
        ),
    ]


def application_cases(n, a):
    ledger, escrow, router = n["ledger"], n["escrow"], n["router"]
    alice, bob, carol = a["alice"], a["bob"], a["carol"]

    def balance(account):
        return f"{ledger}.balances:{account}"

    def allowance(spender):
        return f"{ledger}.allowances:{alice}:{spender}"

    def context(caller, signer):
        return [caller, signer, ledger]

    def transfer(sender, recipient, amount):
        return event(ledger, "Transfer", sender=sender, recipient=recipient, amount=amount)

    return [
        Case(
            "approve-escrow",
            ledger,
            "approve",
            {"spender": escrow, "amount": "30"},
            Decimal("30"),
            {allowance(escrow): "30"},
        ),
        Case(
            "escrow-open",
            escrow,
            "open_order",
            {"order": "purchase", "seller": bob, "amount": "12.5", "seconds": 3600},
            "open",
            {
                balance(alice): "87.5",
                balance(escrow): "12.5",
                allowance(escrow): "17.5",
                f"{ledger}.moves": 1,
                f"{ledger}.last_context": context(escrow, alice),
                f"{escrow}.orders:purchase/status": "open",
            },
            events=(
                transfer(alice, escrow, "12.5"),
                event(escrow, "Opened", order="purchase", buyer=alice),
            ),
        ),
        Case(
            "escrow-duplicate",
            escrow,
            "open_order",
            {"order": "purchase", "seller": bob, "amount": "12.5", "seconds": 3600},
            error="duplicate order",
        ),
        Case(
            "escrow-unauthorized",
            escrow,
            "release",
            {"order": "purchase"},
            actor="bob",
            error="buyer required",
        ),
        Case(
            "escrow-nested-rollback",
            escrow,
            "release",
            {"order": "purchase", "fail": True},
            error="release deliberate",
        ),
        Case(
            "escrow-release",
            escrow,
            "release",
            {"order": "purchase"},
            "released",
            {
                balance(escrow): "0",
                balance(bob): "12.5",
                f"{ledger}.moves": 2,
                f"{escrow}.completed": 1,
                f"{escrow}.orders:purchase/status": "released",
            },
            events=(transfer(escrow, bob, "12.5"), event(escrow, "Released", order="purchase")),
        ),
        Case(
            "escrow-release-replay", escrow, "release", {"order": "purchase"}, error="closed order"
        ),
        Case(
            "escrow-expired-open",
            escrow,
            "open_order",
            {"order": "expired", "seller": bob, "amount": "5", "seconds": 0},
            "open",
            {
                balance(alice): "82.5",
                balance(escrow): "5",
                allowance(escrow): "12.5",
                f"{ledger}.moves": 3,
                f"{escrow}.orders:expired/status": "open",
            },
            events=(
                transfer(alice, escrow, "5"),
                event(escrow, "Opened", order="expired", buyer=alice),
            ),
        ),
        Case(
            "escrow-expired-release", escrow, "release", {"order": "expired"}, error="order expired"
        ),
        Case(
            "escrow-refund",
            escrow,
            "refund",
            {"order": "expired"},
            "refunded",
            {
                balance(alice): "87.5",
                balance(escrow): "0",
                f"{ledger}.moves": 4,
                f"{ledger}.last_context": context(escrow, bob),
                f"{escrow}.orders:expired/status": "refunded",
            },
            actor="bob",
            events=(transfer(escrow, alice, "5"),),
        ),
        Case("escrow-refund-replay", escrow, "refund", {"order": "expired"}, error="closed order"),
        Case(
            "approve-router",
            ledger,
            "approve",
            {"spender": router, "amount": "20"},
            Decimal("20"),
            {allowance(router): "20"},
        ),
        Case(
            "batch-success",
            router,
            "distribute",
            {"target": ledger, "recipients": [bob, carol], "amounts": ["2.25", "3.75"]},
            1,
            {
                balance(alice): "81.5",
                balance(bob): "14.75",
                balance(carol): "3.75",
                allowance(router): "14",
                f"{ledger}.moves": 6,
                f"{ledger}.last_context": context(router, alice),
                f"{router}.runs": 1,
            },
            events=(
                transfer(alice, bob, "2.25"),
                transfer(alice, carol, "3.75"),
                event(router, "Batch", count=2, sender=alice),
            ),
        ),
        Case(
            "batch-second-call-failure",
            router,
            "distribute",
            {"target": ledger, "recipients": [bob, carol], "amounts": ["1", "200"]},
            error="insufficient allowance",
        ),
        Case(
            "batch-recovery",
            router,
            "distribute",
            {"target": ledger, "recipients": [bob, carol], "amounts": ["1", "1"]},
            2,
            {
                balance(alice): "79.5",
                balance(bob): "15.75",
                balance(carol): "4.75",
                allowance(router): "12",
                f"{ledger}.moves": 8,
                f"{router}.runs": 2,
            },
            events=(
                transfer(alice, bob, "1"),
                transfer(alice, carol, "1"),
                event(router, "Batch", count=2, sender=alice),
            ),
        ),
        Case(
            "private-export-denied",
            router,
            "private_call",
            {"target": ledger},
            error="exported function",
        ),
        Case(
            "allowance-denied",
            ledger,
            "transfer_from",
            {"owner": alice, "recipient": carol, "amount": "1"},
            actor="bob",
            error="insufficient allowance",
        ),
        Case(
            "zero-transfer-denied",
            ledger,
            "transfer",
            {"recipient": carol, "amount": "0"},
            actor="bob",
            error="positive amount",
        ),
        Case(
            "ledger-independent-transfer",
            ledger,
            "transfer",
            {"recipient": carol, "amount": "1"},
            Decimal("5.75"),
            {
                balance(bob): "14.75",
                balance(carol): "5.75",
                f"{ledger}.moves": 9,
                f"{ledger}.last_context": context(bob, bob),
            },
            actor="bob",
            events=(transfer(bob, carol, "1"),),
        ),
    ]


def host_cases(n, a, signature):
    inspector, ledger, auth = n["inspector"], n["ledger"], n["commitments"]
    alice, bob, carol = a["alice"], a["bob"], a["carol"]
    result = [alice, Decimal("5.75"), Decimal("100")]
    mirrors = {
        f"{inspector}.mirror:{account}": value
        for account, value in [(alice, "79.5"), (bob, "14.75"), (carol, "5.75"), (n["escrow"], "0")]
    }
    message = "contract-suite authorization"
    return [
        Case(
            "foreign-state-and-copy",
            inspector,
            "inspect",
            {"target": ledger, "account": carol, "accounts": [alice, bob, carol, n["escrow"]]},
            result,
            {f"{inspector}.result": result, **mirrors},
        ),
        Case(
            "foreign-write-denied",
            inspector,
            "forbidden_write",
            {"account": carol},
            error="subscript assignment is not supported",
        ),
        Case(
            "mirror-clear",
            inspector,
            "clear_mirror",
            {"accounts": [alice, bob, carol, n["escrow"]]},
            result=[],
            state={key: None for key in mirrors},
        ),
        Case(
            "signature-invalid",
            auth,
            "authorize",
            {"nonce": "n1", "message": message + "tampered", "signature": signature},
            error="bad signature",
        ),
        Case(
            "signature-valid",
            auth,
            "authorize",
            {"nonce": "n1", "message": message, "signature": signature},
            hashlib.sha3_256(message.encode()).hexdigest(),
            {f"{auth}.used:{alice}:n1": True, f"{auth}.count": 1},
            events=(event(auth, "Accepted", nonce="n1", signer=alice),),
        ),
        Case(
            "signature-replay-denied",
            auth,
            "authorize",
            {"nonce": "n1", "message": message, "signature": signature},
            error="nonce already used",
        ),
        Case(
            "time-random-bridges",
            auth,
            "time_and_random",
            result=[2024, 3, 1, 3],
            state={f"{auth}.result": [2024, 3, 1, 3]},
        ),
    ]


def cases(names, accounts, signature):
    return [
        *simple_cases(names, accounts),
        *application_cases(names, accounts),
        *host_cases(names, accounts, signature),
    ]


def initial_state(names, accounts, scenarios):
    state = {key: None for case in scenarios for key in case.state}
    state.update(
        {
            f"{names['counter']}.value": 2,
            f"{names['counter']}.admin": accounts["alice"],
            f"{names['ledger']}.admin": accounts["alice"],
            f"{names['ledger']}.balances:{accounts['alice']}": "100",
            f"{names['ledger']}.moves": 0,
            f"{names['escrow']}.completed": 0,
            f"{names['router']}.runs": 0,
            f"{names['commitments']}.count": 0,
        }
    )
    return state


def select_state(value, selector):
    for component in selector.split("/")[1:]:
        if value is None:
            return None
        value = value[component]
    return value
