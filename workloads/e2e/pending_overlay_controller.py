spend = Hash(default_value=0)
metadata = Hash(default_value="")


@construct
def seed():
    spend["active"] = 0
    spend["last_before"] = 0
    spend["last_after"] = 0
    spend["last_amount"] = 0
    metadata["last_to"] = ""
    metadata["last_tag"] = ""


@export
def spend_via_adapter(adapter_contract: str, amount: int, recipient: str, tag: str):
    assert amount > 0, "amount must be positive."
    spend["active"] = amount
    metadata["last_tag"] = tag

    adapter = importlib.import_module(adapter_contract)
    adapter_result = adapter.interact(
        {
            "amount": amount,
            "recipient": recipient,
            "tag": tag,
        }
    )

    remaining = spend["active"]
    spend["last_after"] = remaining
    spend["active"] = 0
    return {
        "adapter_result": adapter_result,
        "remaining_after_adapter": remaining,
    }


@export
def get_active_public_spend_remaining():
    return spend["active"]


@export
def adapter_spend_public(amount: int, to: str):
    remaining = spend["active"]
    assert amount > 0, "amount must be positive."
    assert remaining >= amount, "spend budget exceeded."
    spend["last_before"] = remaining
    spend["last_amount"] = amount
    spend["active"] = remaining - amount
    metadata["last_to"] = to
    return spend["active"]


@export
def get_spend_record(key: str):
    return spend[key]


@export
def get_metadata_record(key: str):
    return metadata[key]
