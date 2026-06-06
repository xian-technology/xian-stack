metadata = Hash(default_value="")


@construct
def seed(controller_contract: str):
    assert isinstance(controller_contract, str) and controller_contract != "", (
        "controller_contract is required."
    )
    metadata["controller_contract"] = controller_contract


def controller_module():
    controller_contract = metadata["controller_contract"]
    assert importlib.exists(controller_contract), "controller contract does not exist."
    for export_name in (
        "get_active_public_spend_remaining",
        "adapter_spend_public",
    ):
        assert importlib.has_export(controller_contract, export_name), (
            "controller contract is missing export " + export_name + "."
        )
    return importlib.import_module(controller_contract)


@export
def interact(payload: dict):
    assert isinstance(payload, dict), "payload must be a dict."
    assert ctx.caller == metadata["controller_contract"], (
        "only the configured controller can call interact."
    )

    amount = payload.get("amount")
    assert amount > 0, "amount must be positive."
    recipient = payload.get("recipient")
    assert isinstance(recipient, str) and recipient != "", "recipient is required."

    controller = controller_module()
    before = controller.get_active_public_spend_remaining()
    assert before == amount, "adapter did not see the controller's pending spend budget."

    after = controller.adapter_spend_public(amount=amount, to=recipient)
    final = controller.get_active_public_spend_remaining()
    assert after == final, "controller spend return value drifted."
    assert final == 0, "adapter did not consume the spend budget."

    return {
        "before": before,
        "after": after,
        "recipient": recipient,
        "tag": payload.get("tag", ""),
    }
