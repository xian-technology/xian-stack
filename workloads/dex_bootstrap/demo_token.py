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
metadata = Hash()


@construct
def seed(
    owner: str,
    supply: float,
    token_name: str = "Xian DEX Demo Token",
    token_symbol: str = "XDT",
    precision: int = 8,
):
    assert supply > 0, "Supply must be positive"
    assert precision >= 0, "Precision must be non-negative"

    balances[owner] = supply
    metadata["token_name"] = token_name
    metadata["token_symbol"] = token_symbol
    metadata["token_logo_url"] = ""
    metadata["token_logo_svg"] = ""
    metadata["token_website"] = ""
    metadata["precision"] = precision
    metadata["total_supply"] = supply
    metadata["operator"] = owner


@export
def change_metadata(key: str, value: Any):
    assert ctx.caller == metadata["operator"], "Only operator can set metadata."
    assert key not in ("precision", "total_supply"), "Managed metadata cannot be changed."
    metadata[key] = value


@export
def get_metadata():
    return {
        "token_name": metadata["token_name"],
        "token_symbol": metadata["token_symbol"],
        "token_logo_url": metadata["token_logo_url"],
        "token_logo_svg": metadata["token_logo_svg"],
        "token_website": metadata["token_website"],
        "precision": metadata["precision"],
        "total_supply": metadata["total_supply"],
    }


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
    assert approvals[main_account, ctx.caller] >= amount, "Not enough coins approved to send."
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
