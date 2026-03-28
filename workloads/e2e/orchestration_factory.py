import submission

last_prefix = Variable()
deployed_children = Hash(default_value="")
last_failure_prefix = Variable()


def child_code():
    return '''
construct_meta = Hash()
touch_total = Variable()


@construct
def seed(factory_name: str, role: str):
    construct_meta["factory"] = factory_name
    construct_meta["role"] = role
    construct_meta["caller"] = ctx.caller
    construct_meta["signer"] = ctx.signer
    construct_meta["submission_name"] = ctx.submission_name
    touch_total.set(0)


@export
def get_construct_meta():
    return {
        "factory": construct_meta["factory"],
        "role": construct_meta["role"],
        "caller": construct_meta["caller"],
        "signer": construct_meta["signer"],
        "submission_name": construct_meta["submission_name"],
    }


@export
def touch(account: str, amount: int):
    assert amount > 0, "amount must be positive."
    next_total = (touch_total.get() or 0) + amount
    touch_total.set(next_total)
    return {
        "account": account,
        "amount": amount,
        "total": next_total,
        "this": ctx.this,
        "caller": ctx.caller,
        "signer": ctx.signer,
        "entry": f"{ctx.entry[0]}.{ctx.entry[1]}",
    }


@export
def describe(account: str, amount: int):
    return {
        "account": account,
        "amount": amount,
        "this": ctx.this,
        "caller": ctx.caller,
        "signer": ctx.signer,
        "entry": f"{ctx.entry[0]}.{ctx.entry[1]}",
    }


@export
def ping(label: str):
    return {
        "label": label,
        "this": ctx.this,
        "caller": ctx.caller,
        "signer": ctx.signer,
        "entry": f"{ctx.entry[0]}.{ctx.entry[1]}",
    }


@export
def get_touch_total():
    return touch_total.get()


def internal_secret():
    return "factory-secret"
'''


def bad_child_code():
    return '''
@construct
def seed():
    assert False, "factory child constructor failed"
'''


def remember(prefix: str, first: str, second: str):
    last_prefix.set(prefix)
    deployed_children[prefix, "first"] = first
    deployed_children[prefix, "second"] = second


@export
def deploy_family(prefix: str):
    assert prefix.startswith("con_"), "prefix must start with con_."
    first = prefix + "_alpha"
    second = prefix + "_beta"
    submission.submit_contract(
        name=first,
        code=child_code(),
        constructor_args={"factory_name": ctx.this, "role": "alpha"},
    )
    submission.submit_contract(
        name=second,
        code=child_code(),
        constructor_args={"factory_name": ctx.this, "role": "beta"},
    )
    remember(prefix, first, second)
    return {
        "factory": ctx.this,
        "caller": ctx.caller,
        "signer": ctx.signer,
        "children": [first, second],
    }


@export
def deploy_family_with_failure(prefix: str):
    assert prefix.startswith("con_"), "prefix must start with con_."
    first = prefix + "_good"
    second = prefix + "_bad"
    last_failure_prefix.set(prefix)
    submission.submit_contract(
        name=first,
        code=child_code(),
        constructor_args={"factory_name": ctx.this, "role": "good"},
    )
    submission.submit_contract(
        name=second,
        code=bad_child_code(),
        constructor_args={},
    )


@export
def get_last_family(prefix: str):
    return {
        "prefix": prefix,
        "first": deployed_children[prefix, "first"],
        "second": deployed_children[prefix, "second"],
    }
