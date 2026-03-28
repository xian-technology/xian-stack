@export
def start(
    router_contract: str,
    mid_contract: str,
    leaf_contract: str,
    function_name: str,
    account: str,
    amount: int,
):
    router = importlib.import_module(router_contract)
    return {
        "root_ctx": {
            "this": ctx.this,
            "caller": ctx.caller,
            "signer": ctx.signer,
            "entry": f"{ctx.entry[0]}.{ctx.entry[1]}",
        },
        "nested": router.mid_chain(
            mid_contract=mid_contract,
            leaf_contract=leaf_contract,
            function_name=function_name,
            account=account,
            amount=amount,
        ),
    }
