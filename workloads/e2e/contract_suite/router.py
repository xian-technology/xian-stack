runs = Variable()
Batch = LogEvent("Batch", {"count": int, "sender": str})


@construct
def seed():
    runs.set(0)


@export
def distribute(target: str, recipients: list, amounts: list):
    assert importlib.exists(target), "missing token"
    assert importlib.has_export(target, "transfer_from"), "missing export"
    assert len(recipients) == len(amounts), "length mismatch"
    module = importlib.import_module(target)
    for recipient, amount in zip(recipients, amounts, strict=True):
        module.transfer_from(owner=ctx.caller, recipient=recipient, amount=amount)
    runs.set(runs.get() + 1)
    Batch({"count": len(recipients), "sender": ctx.caller})
    return runs.get()


@export
def private_call(target: str):
    return importlib.call(
        target, "move", {"sender": ctx.caller, "recipient": ctx.this, "amount": 1}
    )
