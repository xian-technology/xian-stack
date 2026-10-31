foreign_admin = ForeignVariable(foreign_contract="__LEDGER__", foreign_name="admin")
foreign_balances = ForeignHash(foreign_contract="__LEDGER__", foreign_name="balances")
mirror = Hash()
result = Variable()


@export
def inspect(target: str, account: str, accounts: list):
    dynamic = ForeignHash(foreign_contract=target, foreign_name="balances")
    snapshot = foreign_balances.all()
    for member in accounts:
        mirror[member] = foreign_balances[member]
    module = importlib.import_module(target)
    assert dynamic[account] == foreign_balances[account] == module.balance(account=account)
    assert importlib.call(target, "balance", {"account": account}) == dynamic[account]
    assert Contract.get_info(target)["owner"] == importlib.owner_of(target)
    answer = [foreign_admin.get(), dynamic[account], sum(snapshot)]
    result.set(answer)
    return answer


@export
def forbidden_write(account: str):
    result.set("corrupted")
    foreign_balances[account] = 999


@export
def clear_mirror(accounts: list):
    for member in accounts:
        mirror[member] = None
    return mirror.all()
