@export
def dynamic_touch(
    target_contract: str,
    function_name: str,
    account: str,
    amount: int,
):
    return {
        "router_ctx": {
            "this": ctx.this,
            "caller": ctx.caller,
            "signer": ctx.signer,
            "entry": f"{ctx.entry[0]}.{ctx.entry[1]}",
        },
        "result": importlib.call(
            target_contract,
            function_name,
            {"account": account, "amount": amount},
        ),
    }


@export
def dynamic_ping_module(target_contract: str, label: str):
    target = importlib.import_module(target_contract)
    return {
        "router_ctx": {
            "this": ctx.this,
            "caller": ctx.caller,
            "signer": ctx.signer,
            "entry": f"{ctx.entry[0]}.{ctx.entry[1]}",
        },
        "result": importlib.call(target, "ping", {"label": label}),
    }


@export
def private_probe(target_contract: str, function_name: str):
    return importlib.call(target_contract, function_name, {})


@export
def mid_chain(
    mid_contract: str,
    leaf_contract: str,
    function_name: str,
    account: str,
    amount: int,
):
    before = {
        "this": ctx.this,
        "caller": ctx.caller,
        "signer": ctx.signer,
        "entry": f"{ctx.entry[0]}.{ctx.entry[1]}",
    }
    result = importlib.call(
        mid_contract,
        "forward",
        {
            "leaf_contract": leaf_contract,
            "function_name": function_name,
            "account": account,
            "amount": amount,
        },
    )
    after = {
        "this": ctx.this,
        "caller": ctx.caller,
        "signer": ctx.signer,
        "entry": f"{ctx.entry[0]}.{ctx.entry[1]}",
    }
    return {
        "router_before": before,
        "router_after": after,
        "nested": result,
    }
