# ruff: noqa: F821
results = Hash(default_value="")


@construct
def seed():
    pass


@export
def crunch(slot: str, payload: str, rounds: int):
    assert rounds > 0, "rounds must be positive"
    assert rounds <= 256, "rounds too large"

    digest = payload
    for round_index in range(rounds):
        digest = hashlib.sha256_text(digest)

    results[slot] = digest
    return digest
