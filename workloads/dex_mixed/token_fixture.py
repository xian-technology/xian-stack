# ruff: noqa: F821
Transfer = LogEvent(
    event="Transfer",
    params={
        "from": {"type": str, "idx": True},
        "to": {"type": str, "idx": True},
        "amount": {"type": (int, float, decimal)},
    },
)

Approve = LogEvent(
    event="Approve",
    params={
        "from": {"type": str, "idx": True},
        "to": {"type": str, "idx": True},
        "amount": {"type": (int, float, decimal)},
    },
)

balances = Hash(default_value=0.0)
approvals = Hash(default_value=0.0)
metadata = Hash(default_value="")


@construct
def seed(owner: str, supply: float, name: str, symbol: str):
    assert supply > 0, "Supply must be positive"
    balances[owner] = supply
    metadata["name"] = name
    metadata["symbol"] = symbol


@export
def transfer(amount: float, to: str):
    assert amount > 0, "Amount must be positive"
    assert balances[ctx.caller] >= amount, "Insufficient balance"

    balances[ctx.caller] -= amount
    balances[to] += amount
    Transfer({"from": ctx.caller, "to": to, "amount": amount})


@export
def approve(amount: float, to: str):
    assert amount >= 0, "Amount must be non-negative"
    approvals[ctx.caller, to] = amount
    Approve({"from": ctx.caller, "to": to, "amount": amount})


@export
def transfer_from(amount: float, to: str, main_account: str):
    assert amount > 0, "Amount must be positive"
    assert approvals[main_account, ctx.caller] >= amount, (
        "Not enough coins approved to send!"
    )
    assert balances[main_account] >= amount, "Insufficient balance"

    approvals[main_account, ctx.caller] -= amount
    balances[main_account] -= amount
    balances[to] += amount
    Transfer({"from": main_account, "to": to, "amount": amount})


@export
def balance_of(address: str):
    return balances[address]


@export
def allowance(owner: str, spender: str):
    return approvals[owner, spender]
