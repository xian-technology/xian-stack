counter = Variable()
claims = Hash(default_value=False)

ClaimedEvent = LogEvent(
    event="Claimed",
    params={
        "slot": {"type": str, "idx": True},
        "claimer": {"type": str, "idx": True},
        "amount": {"type": int},
    },
)


@construct
def seed():
    counter.set(0)


@export
def claim(slot: str, amount: int = 1):
    assert slot != "", "slot is required."
    assert amount > 0, "amount must be positive."
    assert claims[slot] is False, "slot already claimed."

    claims[slot] = True
    next_counter = (counter.get() or 0) + amount
    counter.set(next_counter)
    ClaimedEvent(
        {
            "slot": slot,
            "claimer": ctx.caller,
            "amount": amount,
        }
    )
    return next_counter


@export
def current():
    return counter.get()
