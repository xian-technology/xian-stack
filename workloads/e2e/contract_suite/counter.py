value = Variable()
admin = Variable()
history = Hash()
Changed = LogEvent("Changed", {"value": int, "caller": str})


@construct
def seed(owner: str, initial: int = 2):
    admin.set(owner)
    value.set(initial)


@export(typecheck=True)
def add(delta: int):
    assert delta > 0, "positive delta required"
    value.set(value.get() + delta)
    history["recent", "value"] = value.get()
    Changed({"value": value.get(), "caller": ctx.caller})
    return value.get()


@export
def reset():
    assert ctx.caller == admin.get(), "admin required"
    history["recent", "value"] = None
    value.set(0)
    return len(history.all())


@export
def fail_after_write():
    value.set(999)
    Changed({"value": 999, "caller": ctx.caller})
    raise Exception("counter deliberate failure")
