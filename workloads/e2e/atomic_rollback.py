# ruff: noqa: F821
import currency

records = Hash(default_value=None)
attempts = Variable()

RollbackProbeEvent = LogEvent(
    event="RollbackProbe",
    params={
        "key": {"type": str, "idx": True},
        "stage": {"type": str, "idx": True},
        "value": {"type": int},
    },
)


@construct
def seed():
    attempts.set(0)


def record_attempt(key: str, stage: str, value: int):
    attempts.set((attempts.get() or 0) + 1)
    records[key, "stage"] = stage
    records[key, "value"] = value
    RollbackProbeEvent({"key": key, "stage": stage, "value": value})


@export
def set_record(key: str, value: int):
    assert key != "", "key required."
    record_attempt(key, "committed", value)
    return records[key, "value"]


@export
def mutate_then_assert(key: str, value: int):
    record_attempt(key, "assert", value)
    assert False, "intentional rollback assertion."


@export
def mutate_then_overdraw(key: str, to: str, amount: int):
    record_attempt(key, "overdraw", amount)
    currency.transfer(amount=amount, to=to)
    return records[key, "value"]


@export
def mutate_then_type_error(key: str, value: int):
    record_attempt(key, "type-error", value)
    return value / 0


@export
def get_record(key: str):
    return {
        "stage": records[key, "stage"],
        "value": records[key, "value"],
    }


@export
def get_attempts():
    return attempts.get()
