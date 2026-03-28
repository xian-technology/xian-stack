@export
def forward(
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
        leaf_contract,
        function_name,
        {"account": account, "amount": amount},
    )
    after = {
        "this": ctx.this,
        "caller": ctx.caller,
        "signer": ctx.signer,
        "entry": f"{ctx.entry[0]}.{ctx.entry[1]}",
    }
    return {
        "mid_before": before,
        "mid_after": after,
        "leaf": result,
    }
