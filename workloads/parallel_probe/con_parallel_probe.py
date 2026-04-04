values = Hash(default_value=0)
observations = Hash(default_value=0)
flag = Variable()


@construct
def seed():
    flag.set(0)


@export
def write_value(key: str, value: int):
    values[key] = value
    return values[key]


@export
def set_flag(value: int):
    flag.set(value)
    return flag.get()


@export
def observe_flag(tag: str):
    observed = flag.get() or 0
    observations[tag] = observed
    return observed


@export
def snapshot_sum(tag: str):
    total = 0
    for value in values.all():
        total += value
    observations[tag] = total
    return total


@export
def get_observation(tag: str):
    return observations[tag]
