foreign_admin = ForeignVariable(foreign_contract="__LEDGER__", foreign_name="admin")
foreign_balances = ForeignHash(foreign_contract="__LEDGER__", foreign_name="balances")
mirror = Hash()
result = Variable()
Injected = LogEvent("Injected", {"count": int})


@export
def inspect(target: str, account: str, accounts: list):
    dynamic = ForeignHash(foreign_contract=target, foreign_name="balances")
    snapshot = foreign_balances.all()
    mirror.clone_from(foreign_balances)
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
    mirror.clear()
    return mirror.all()


@export
def echo_value(value: Any):
    return [value, str(value), isinstance(value, decimal)]


@export
def consume_payload(payload: dict):
    result.set("cross-contract callee entered")
    if "callback" in payload:
        return payload["callback"][0]()
    payload["items"].append(5)
    return payload["items"]


@export
def inject_capability(values: list):
    result.set("callee injection write must roll back")
    values.append(result.get)
    Injected({"count": len(values)})
