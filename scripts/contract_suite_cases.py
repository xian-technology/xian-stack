"""Authored contracts and explicit outcomes shared by native and five-node tests."""

import hashlib
from dataclasses import dataclass, field
from decimal import Decimal as RawDecimal
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
    "compatibility",
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
        role: (SOURCE_DIR / f"{role}.py")
        .read_text()
        .replace("__LEDGER__", names["ledger"])
        .replace("__INSPECTOR__", names["inspector"])
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


def compatibility_cases(n):
    c = n["compatibility"]

    def success(label, function, result, kwargs=None):
        return Case(label, c, function, kwargs or {}, result, {f"{c}.result": result})

    return [
        success("ordered-module-bindings", "bindings", [1, 2, 3]),
        success(
            "float-set-order-and-identity",
            "float_sets",
            [
                ["-inf", "-10.0", "-2.0", "2.0", "10.0", "inf"],
                ["-inf", "-10.0", "-2.0", "2.0", "10.0", "inf"],
                ["-inf", "-10.0", "-2.0", "2.0", "10.0", "inf"],
                "0.0",
                "-0.0",
                "2.0",
                ["1.0", "10.0", "nan", "nan"],
                True,
                True,
                True,
                False,
                False,
            ],
        ),
        success("starred-calls-and-unpacking", "unpacking", [0, [1, 2, 3], 4, 16]),
        success(
            "streaming-methods-and-aliases",
            "methods",
            [["first", "second", "shared"], [3], [3, 2, 1]],
        ),
        success("unvisited-key-replacement", "mutation", ["a", "c"], {"visited": False}),
        Case(
            "visited-key-replacement-rollback",
            c,
            "mutation",
            {"visited": True},
            error="dictionary keys changed",
        ),
        success("dictionary-compaction", "compaction", ["a", "c", "d", "e", "z"]),
        success("unicode-numeric-strings", "unicode_numbers", [12, 15, 120.0]),
        Case(
            "decimal-quantum-and-boundaries",
            c,
            "quantum",
            result=[
                RawDecimal("2.00"),
                RawDecimal("-0.00"),
                "2.00",
                "2.000",
                "4.00",
                [RawDecimal("2.00"), "2.00", False],
            ],
            state={f"{c}.result": "2", f"{c}.fixed": "2", f"{c}.entries:quantum": "-0.00"},
        ),
        Case("typed-variable-rollback", c, "typed_failure", error="type"),
        Case(
            "bulk-prefix-and-self-clone",
            c,
            "bulk",
            result=[9, 0, 2, [9, 1, 2]],
            state={
                f"{c}.result": [9, 0, 2, [9, 1, 2]],
                f"{c}.entries:a": 9,
                f"{c}.entries:a:x": None,
                f"{c}.entries:b:y": 2,
                f"{c}.copy:a": 9,
                f"{c}.copy:a:x": 1,
                f"{c}.copy:b:y": 2,
                f"{c}.copy:quantum": "-0.00",
            },
        ),
        Case("bulk-clear-rollback", c, "bulk_failure", error="bulk deliberate"),
        Case("malformed-hex-rollback", c, "malformed", {"kind": "hex"}, error="hex"),
        Case("short-starred-rollback", c, "malformed", {"kind": "unpack"}, error="unpack"),
        Case("decimal-precision-rollback", c, "malformed", {"kind": "decimal"}, error="decimal"),
        success("compatibility-recovery", "bindings", [1, 2, 3]),
    ]


def decimal_cases(n):
    """R17 results copied from historical contracting at 3b82001f, Python 3.12.10."""
    c = n["compatibility"]
    successes = [
        (
            "decimal-classification",
            "decimal_classification",
            [
                "True",
                "True",
                "False",
                "False",
                "True",
                "False",
                "False",
                "False",
                "False",
                "False",
                "+Normal",
                "2.500",
                "2.500",
                "2.500",
                "-2.500",
                "-2.500",
            ],
        ),
        (
            "decimal-quantization",
            "decimal_quantization",
            ["2.5", "2.50", "False", "2.500", "2", "2", "2"],
        ),
        (
            "decimal-math",
            "decimal_math",
            [
                "0",
                "12.18249396070347343807017595116796618318276779006316131156039834183818512614331441006025552",
                "4.20000",
                "0.9162907318741550651835272117680110714501012199082624677919678819807853657379630490242705511",
                "0.3979400086720376095725222105510139464636202370757829173791450777457836214511509810261454958",
                "0",
                "1.581138830084189665999446772216359266859777569662608413428752426396297219319619110672124054",
                "10",
                "0.100",
            ],
        ),
        (
            "decimal-ordering",
            "decimal_ordering",
            [
                "1",
                "1",
                "1",
                "1",
                "2.500",
                "2.500",
                "1.20",
                "-1.20",
                "2.499999999999999999999999999999999999999999999999999999999999999999999999999999999999999999",
                "2.500000000000000000000000000000000000000000000000000000000000000000000000000000000000000001",
                "2.499999999999999999999999999999999999999999999999999999999999999999999999999999999999999999",
                "25.000",
                "250.0",
                "25.000",
            ],
        ),
        (
            "decimal-logical",
            "decimal_logical",
            [
                "1",
                "111",
                "110",
                "1111111111111111111111111111111111111111111111111111111111111111111111111111111111111111010",
            ],
        ),
        (
            "decimal-general-powers",
            "decimal_powers",
            [
                "2.297396709994070013597253893555855178887701778195595011027422236987206412507026113622946226",
                "0.4352752816480620695681350087398730494895627121740015241209297842533750088762390050941893824",
            ],
        ),
        (
            "decimal-tuple-exact-construction",
            "decimal_tuple_exact",
            [0, [2, 5, 0, 0], -3, "0.1000000000000000055511151231257827021181583404541015625"],
        ),
        (
            "decimal-nonfinite-transport",
            "decimal_nonfinite",
            ["Infinity", "-Infinity", "Infinity", True, True, False, "NaN", True, True],
        ),
        (
            "decimal-nan-identity",
            "decimal_nan_identity",
            [False, True, True, True, True, True, 2, 2, False],
        ),
    ]
    result = [
        Case(label, c, function, result=value, state={f"{c}.result": value})
        for label, function, value in successes
    ]
    result.append(
        Case("decimal-negative-fractional-domain", c, "decimal_negative_power", error="invalid")
    )
    for kind, target, nested in [
        ("infinity", "variable", False),
        ("nan", "variable", True),
        ("nan", "hash", False),
        ("infinity", "hash", True),
    ]:
        label = f"decimal-{kind}-{target}-{'nested' if nested else 'direct'}"
        result.append(
            Case(
                label + "-rollback",
                c,
                "decimal_nonfinite_storage",
                {"kind": kind, "target": target, "nested": nested},
                error="finite",
            )
        )
        marker = label + "-recovered"
        result.append(
            Case(
                label + "-recovery",
                c,
                "decimal_recovery",
                {"marker": marker},
                marker,
                {
                    f"{c}.result": marker,
                    f"{c}.entries:recovery": marker,
                    f"{c}.entries:nonfinite": None,
                },
            )
        )
    return result


def surface_cases(names):
    contract = names["compatibility"]
    result = [
        False,
        False,
        True,
        "x",
        "ος",
        "\u1c89",
        True,
        "message",
        True,
        True,
        [1900, 1, 1, 23, 45, 120000],
        [-1, -1, -1, -1, -1],
        "-1 day, 23:59:59",
    ]

    def success(label):
        return Case(
            label,
            contract,
            "surface_boundaries",
            result=result,
            state={f"{contract}.result": result},
        )

    scenarios = [success("allowed-surface-boundaries")]
    for mode, error in [
        ("classinfo", "classinfo"),
        ("datetime", "invalid datetime"),
        ("format", "numbering"),
    ]:
        scenarios.append(
            Case(
                f"surface-{mode}-rollback",
                contract,
                "surface_boundaries",
                {"failure": mode},
                error=error,
            )
        )
        scenarios.append(success(f"surface-{mode}-recovery"))
    return scenarios


def cases(names, accounts, signature):
    return [
        *simple_cases(names, accounts),
        *application_cases(names, accounts),
        *host_cases(names, accounts, signature),
        *compatibility_cases(names),
        *decimal_cases(names),
        *surface_cases(names),
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
