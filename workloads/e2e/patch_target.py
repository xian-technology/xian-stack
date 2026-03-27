mode = Variable()
patch_count = Variable()


@construct
def seed():
    mode.set("baseline")
    patch_count.set(0)


@export
def get_status():
    return {
        "mode": mode.get(),
        "patch_count": patch_count.get(),
    }
