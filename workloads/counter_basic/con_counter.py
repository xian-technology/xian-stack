# ruff: noqa: F821
v = Variable()


@construct
def seed():
    v.set(0)


@export
def increment():
    v.set(v.get() + 1)
    return v.get()


@export
def add(amount: float):
    v.set(v.get() + amount)
    return v.get()


@export
def get():
    return v.get()
