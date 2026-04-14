meta = Variable()
touch_total = Variable()


@construct
def seed(factory_name: str, role: str, should_fail: bool = False):
    assert not should_fail, "factory child constructor failed"
    meta.set([ctx.caller, ctx.signer, ctx.submission_name])
    touch_total.set(0)


@export
def get_construct_meta():
    return meta.get()


@export
def touch(account: str, amount: int):
    assert amount > 0, "amount must be positive."
    touch_total.set((touch_total.get() or 0) + amount)
    return touch_total.get()


@export
def describe(account: str, amount: int):
    return {
        "caller": ctx.caller,
        "signer": ctx.signer,
        "entry": f"{ctx.entry[0]}.{ctx.entry[1]}",
    }


@export
def ping(label: str):
    return label


@export
def get_touch_total():
    return touch_total.get()


def internal_secret():
    return "factory-secret"
