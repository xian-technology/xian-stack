"""Contract calls and transactions for the stack's offline checks, run with
the node's own engine (vm-core's Rust executor)."""

from types import SimpleNamespace


def execute_contract(
    *,
    driver,
    contract_name,
    function_name,
    kwargs,
    context,
    meter=False,
    chi_budget_raw=0,
):
    """One call by the block executor's rules (``xian_vm_core.execute_tx``)
    on a fork of ``driver``'s state, without fees: the effects are returned,
    not staged."""
    from contracting.runtime_features import RUNTIME_FEATURE_ZK, runtime_feature_without_state
    from xian_vm_core import execute_tx

    environment, overlay = driver.committed_storage()
    return execute_tx(
        environment,
        overlay.snapshot(),
        sender=context.get("signer") or "sys",
        contract_name=contract_name,
        function_name=function_name,
        kwargs=kwargs,
        context=context,
        meter=meter,
        chi_budget=-(-int(chi_budget_raw) // 1000),
        zk_enabled_without_state=runtime_feature_without_state(driver, RUNTIME_FEATURE_ZK),
    )


def process_transaction(driver, tx, fee_policy):
    """``tx`` in a one-transaction block as FinalizeBlock processes it; its
    effects stay staged in ``driver``. Returns the receipt (``tx_result``)."""
    from xian.processor import TxProcessor
    from xian.utils.tx import transaction_bytes

    processor = TxProcessor(client=SimpleNamespace(raw_driver=driver))
    [response] = processor.process_block([tx], [transaction_bytes(tx)], fee_policy=fee_policy)
    return response["tx_result"]
