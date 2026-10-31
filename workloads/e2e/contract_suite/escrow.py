import __LEDGER__ as token

orders = Hash()
completed = Variable()
Opened = LogEvent("Opened", {"order": str, "buyer": str})
Released = LogEvent("Released", {"order": str})


@construct
def seed():
    completed.set(0)


@export
def open_order(order: str, seller: str, amount: str, seconds: int):
    assert orders[order] is None, "duplicate order"
    assert seconds >= 0, "negative duration"
    token.transfer_from(owner=ctx.caller, recipient=ctx.this, amount=amount)
    orders[order] = {
        "buyer": ctx.caller,
        "seller": seller,
        "amount": amount,
        "deadline": now + datetime.timedelta(seconds=seconds),
        "status": "open",
    }
    Opened({"order": order, "buyer": ctx.caller})
    return "open"


@export
def release(order: str, fail: bool = False):
    item = orders[order]
    assert item["status"] == "open", "closed order"
    assert ctx.caller == item["buyer"], "buyer required"
    assert now < item["deadline"], "order expired"
    item["status"] = "released"
    orders[order] = item
    token.transfer(recipient=item["seller"], amount=item["amount"])
    completed.set(completed.get() + 1)
    Released({"order": order})
    assert not fail, "release deliberate failure"
    return item["status"]


@export
def refund(order: str):
    item = orders[order]
    assert item["status"] == "open", "closed order"
    assert now >= item["deadline"], "not expired"
    item["status"] = "refunded"
    orders[order] = item
    token.transfer(recipient=item["buyer"], amount=item["amount"])
    return item["status"]


@export
def status(order: str):
    return orders[order]["status"]
