# ruff: noqa: F821

values = Hash(default_value=0)
observations = Hash(default_value=0)
flags = Hash(default_value=0)


@construct
def seed():
    pass


@export
def write_value(group: str, key: str, value: int):
    values[group, key] = value
    return values[group, key]


@export
def set_flag(group: str, value: int):
    flags[group] = value
    return flags[group]


@export
def observe_flag(group: str, tag: str):
    observed = flags[group] or 0
    observations[tag] = observed
    return observed


@export
def snapshot_sum(group: str, tag: str):
    total = 0
    for value in values.all(group):
        total += value
    observations[tag] = total
    return total


@export
def get_observation(tag: str):
    return observations[tag]
