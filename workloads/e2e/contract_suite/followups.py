import __INSPECTOR__ as inspector

result = Variable()
persisted = Variable()
frozen = Variable()
slot = Hash(default_value=0)
shadow = 41
Attempted = LogEvent("Attempted", {"mode": str})


def save(value):
    result.set(value)
    return value


def argument_failure():
    raise Exception("argument evaluated before unbound receiver")


@export
def scopes():
    outer = 9
    values = [outer for outer in [1, 2]]
    nested = [[outer for outer in outer] for outer in [[1, 2], [3]]]
    slot = {"x": 7}
    slot["x"] += 2
    return save([shadow, outer, values, nested, slot["x"]])


@export
def hash_inputs():
    text = "é" * 8192
    digest = hashlib.sha256_text
    return save([digest(text), hashlib.sha3_text(text), hashlib.sha256_hex("616263")])


@export
def hash_callbacks():
    text = "é" * 8192
    result.set("pending hash callbacks")
    Attempted({"mode": "hash budget"})
    answer = list(map(hashlib.sha3_text, [text, text]))
    return save(answer)


@export
def store_sets(label: str = "tuple"):
    value = set([(label, 7), frozenset([2, 3]), round(decimal("1.2300"), 4)])
    persisted.set(value)
    frozen.set(frozenset(value))
    return save([len(value), (label, 7) in value, frozenset([3, 2]) in value])


@export
def read_sets():
    value = persisted.get()
    immutable = frozen.get()
    before = len(value)
    value.add(("local only", 8))
    raw = [item for item in immutable if not isinstance(item, (tuple, frozenset))][0]
    return save(
        [
            isinstance(value, set),
            isinstance(immutable, frozenset),
            before,
            len(value),
            len(persisted.get()),
            ("tuple", 7) in immutable,
            str(raw),
            raw.as_tuple().exponent,
        ]
    )


@export
def descriptors():
    args = ["payload"]
    function = importlib.Func("consume_payload", args)
    alias = function
    args.append("extra")
    shared_args = function.args is args
    function.name = "changed"
    function.args = ("payload",)
    function.name = "consume_payload"
    variable = importlib.Var("slot", Hash)
    variable_alias = variable
    variable.name = "__result"
    variable.type = importlib.Var("result", Variable).type
    is_match = function.is_of
    matches = is_match(inspector.consume_payload)
    module = importlib.import_module("__INSPECTOR__")
    accepted = importlib.enforce_interface(module, [function, variable])
    function.args = ("wrong",)
    rejected = importlib.enforce_interface(module, [function])
    other = importlib.Func("consume_payload", ("wrong",))
    keys = {function: 7}
    return save(
        [
            alias is function,
            shared_args,
            args,
            matches,
            accepted,
            rejected,
            function == other,
            keys[alias],
            variable_alias.name,
            variable_alias.type is Variable,
            variable.is_of(result),
        ]
    )


@export
def callable_keys():
    left = []
    right = []
    first = left.append
    second = right.append
    keys = {}.keys() | []
    keys.update([first, 2, second, None, left.append])
    labels = []
    for key in keys:
        if key is None:
            labels.append("none")
        elif key == 2:
            labels.append("two")
        elif key == first:
            labels.append("first")
        else:
            labels.append("second")
    before = [len(keys), left.append in keys, right.append in keys]
    keys.remove(None)
    keys.remove(2)
    keys.remove(left.append)
    callback = keys.pop()
    callback(9)
    descriptor = importlib.Func("f")
    other = importlib.Func("f")
    descriptors = {descriptor: 1, other: 2}.keys() | []
    descriptor.name = "changed"
    return save(
        [labels, before, left, right, len(keys), len(descriptors), descriptor in descriptors]
    )


def fail_scope(mode):
    if mode == "scope_local":
        answer = shadow  # noqa: F823 - exercise unbound-local rejection
        shadow = 7
        return answer
    if mode == "scope_comprehension":
        return [item for item in [1] for shadow in shadow]
    if mode == "scope_call":
        answer = len(argument_failure())  # noqa: F823 - callee lookup precedes arguments
        len = 7  # noqa: F841 - this binding makes the preceding callee local
        return answer
    if mode == "scope_storage":
        answer = slot.get(argument_failure())  # noqa: F823 - storage shadow must stay local
        slot = {}  # noqa: F841 - this binding makes the preceding receiver local
        return answer
    raise Exception("unknown lexical failure mode")


@export
def fail(mode: str):
    result.set("pending followup must roll back")
    Attempted({"mode": mode})
    if mode.startswith("scope_"):
        return fail_scope(mode)
    if mode == "crypto_key":
        return crypto.verify("ab" * 33, "ok", "00" * 64)
    if mode == "crypto_signature":
        return crypto.verify("ab" * 32, "ok", "00" * 65)
    if mode == "descriptor_storage":
        return persisted.set({"descriptor": importlib.Func("f")})
    if mode == "unsafe_orm_alias":
        constructor = importlib.Var("x", Variable).type
        return constructor()
    if mode == "callable_wrapper":
        return set({save: 1}.keys() | [])
    if mode == "callable_builtin_storage":
        return persisted.set({"keys": {save: 1}.keys() | []})
    raise Exception("unknown followup mode")


@export
def recover(label: str):
    return save(label)
