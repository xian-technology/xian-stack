import submission

last_prefix = Variable()
deployed_children = Hash(default_value="")
last_failure_prefix = Variable()

CHILD_SOURCE = __ORCH_CHILD_SOURCE_JSON__


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
        code=CHILD_SOURCE,
        constructor_args={"factory_name": ctx.this, "role": "alpha"},
    )
    submission.submit_contract(
        name=second,
        code=CHILD_SOURCE,
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
        code=CHILD_SOURCE,
        constructor_args={"factory_name": ctx.this, "role": "good"},
    )
    submission.submit_contract(
        name=second,
        code=CHILD_SOURCE,
        constructor_args={
            "factory_name": ctx.this,
            "role": "bad",
            "should_fail": True,
        },
    )


@export
def get_last_family(prefix: str):
    return {
        "prefix": prefix,
        "first": deployed_children[prefix, "first"],
        "second": deployed_children[prefix, "second"],
    }
