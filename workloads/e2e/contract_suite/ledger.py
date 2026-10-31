admin = Variable()
balances = Hash(default_value=0)
allowances = Hash(default_value=0)
moves = Variable()
last_context = Variable()
Transfer = LogEvent("Transfer", {"sender": str, "recipient": str, "amount": decimal})


@construct
def seed(owner: str):
    admin.set(owner)
    balances[owner] = decimal("100")
    moves.set(0)


def move(sender, recipient, amount):
    assert amount > 0, "positive amount required"
    assert balances[sender] >= amount, "insufficient balance"
    balances[sender] -= amount
    balances[recipient] += amount
    moves.set(moves.get() + 1)
    last_context.set([ctx.caller, ctx.signer, ctx.this])
    Transfer({"sender": sender, "recipient": recipient, "amount": amount})
    return balances[recipient]


@export
def approve(spender: str, amount: str):
    value = decimal(amount)
    assert value >= 0, "negative allowance"
    allowances[ctx.caller, spender] = value
    return value


@export
def transfer(recipient: str, amount: str):
    return move(ctx.caller, recipient, decimal(amount))


@export
def transfer_from(owner: str, recipient: str, amount: str):
    value = decimal(amount)
    assert allowances[owner, ctx.caller] >= value, "insufficient allowance"
    allowances[owner, ctx.caller] -= value
    return move(owner, recipient, value)


@export
def balance(account: str):
    return balances[account]
