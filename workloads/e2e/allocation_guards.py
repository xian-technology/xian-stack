last_status = Variable()


@construct
def seed():
    last_status.set("ready")


@export
def get_last_status():
    return last_status.get()


@export
def small_bytes(size: int):
    payload = bytes(size)
    last_status.set(f"bytes:{len(payload)}")
    return len(payload)


@export
def explode_range(size: int):
    payload = [item for item in range(size)]
    last_status.set(f"range:{len(payload)}")
    return len(payload)


@export
def explode_bytes(size: int):
    payload = bytes(size)
    last_status.set(f"bytes:{len(payload)}")
    return len(payload)


@export
def explode_bytearray(size: int):
    payload = bytearray(size)
    last_status.set(f"bytearray:{len(payload)}")
    return len(payload)


@export
def explode_string_repeat(count: int):
    payload = "ab" * count
    last_status.set(f"string:{len(payload)}")
    return len(payload)


@export
def explode_list_repeat(count: int):
    payload = [1, 2] * count
    last_status.set(f"list:{len(payload)}")
    return len(payload)
