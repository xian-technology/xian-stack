"""Read the stack's genesis fee policy inputs from its environment.

The fee policy is chain state written at genesis, so these settings only
shape genesis creation (a bundle genesis or a localnet); they never reach
an existing node's xian.toml.
"""

from __future__ import annotations

from collections.abc import Mapping

SUPPORTED_TX_FEE_MODES = ("free_metered", "paid_metered")
FEE_POLICY_ENVIRONMENT = (
    ("XIAN_TX_FEE_MODE", "tx_fee_mode", "--tx-fee-mode"),
    ("XIAN_FREE_TX_MAX_CHI", "free_tx_max_chi", "--free-tx-max-chi"),
    ("XIAN_FREE_BLOCK_MAX_CHI", "free_block_max_chi", "--free-block-max-chi"),
)


def _positive_int(name: str, value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise ValueError(f"{name} must be a positive integer") from None
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def genesis_fee_policy(environment: Mapping[str, str]) -> dict[str, object]:
    """Explicitly set fee policy fields; empty means the paid_metered genesis default."""
    policy: dict[str, object] = {}
    for variable, field, _ in FEE_POLICY_ENVIRONMENT:
        value = environment.get(variable, "").strip()
        if not value:
            continue
        if field == "tx_fee_mode":
            if value not in SUPPORTED_TX_FEE_MODES:
                raise ValueError(f"{variable} must be one of {', '.join(SUPPORTED_TX_FEE_MODES)}")
            policy[field] = value
        else:
            policy[field] = _positive_int(variable, value)
    if set(policy) - {"tx_fee_mode"} and policy.get("tx_fee_mode") != "free_metered":
        raise ValueError(
            "XIAN_FREE_TX_MAX_CHI and XIAN_FREE_BLOCK_MAX_CHI require XIAN_TX_FEE_MODE=free_metered"
        )
    return policy


def genesis_fee_policy_arguments(environment: Mapping[str, str]) -> list[str]:
    policy = genesis_fee_policy(environment)
    return [
        argument
        for _, field, option in FEE_POLICY_ENVIRONMENT
        if field in policy
        for argument in (option, str(policy[field]))
    ]
