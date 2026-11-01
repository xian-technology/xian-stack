import __INSPECTOR__ as inspector
import submission

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


Typed = LogEvent("Typed", {"value": indexed(int)})
Text = LogEvent("Text", {"value": str})


@export
def event_helpers():
    Attempted.write_event(event_data={"mode": "bound"})
    write = Attempted.write_event
    write({"mode": "aliased"})
    event_type = importlib.Var("event", LogEvent).type
    event_type.write_event(Attempted, {"mode": "class"})
    emit = Attempted
    emit(data={"mode": "call"})
    Typed.write_event({"value": 7})
    Text.write_event({"value": "é" * 512})
    return save([7, 1024])


@export
def orm_helpers():
    variable_type = importlib.Var("value", Variable).type
    variable_type.set(result, "local")
    read = variable_type.get
    local = read(result)
    foreign = ForeignVariable(foreign_contract="__LEDGER__", foreign_name="admin")
    admin = read(foreign)
    foreign_hash = ForeignHash(foreign_contract="__LEDGER__", foreign_name="balances")
    hash_type = importlib.Var("items", Hash).type
    clone = hash_type.clone_from
    clone(slot, foreign_hash)
    total = sum(hash_type.all(slot))
    hash_type.clear(slot)
    return save([local, admin, total == 100, hash_type.all(slot)])


@export
def interface_callback_order():
    module = importlib.import_module("__INSPECTOR__")
    descriptor = importlib.Func("missing_member")
    descriptor.is_of = 7
    missing = importlib.enforce_interface(module, [descriptor])
    descriptor.name = "interface_none"
    empty = importlib.enforce_interface(module, [descriptor])
    descriptor.name = "interface_constant"
    constant = importlib.enforce_interface(module, [descriptor])
    return save([missing, empty, constant])


@export
def fail_adapter(mode: str):
    result.set("pending adapter must roll back")
    Attempted({"mode": mode})
    variable_type = importlib.Var("value", Variable).type
    hash_type = importlib.Var("items", Hash).type
    if mode == "event_type":
        return Typed.write_event({"value": "wrong"})
    if mode == "event_oversize":
        return Text.write_event({"value": "é" * 513})
    if mode == "orm_wrong_receiver":
        return variable_type.get(slot)
    if mode == "orm_foreign_variable":
        foreign = ForeignVariable(foreign_contract="__LEDGER__", foreign_name="admin")
        return variable_type.set(foreign, "corrupted")
    if mode == "orm_foreign_clear":
        foreign = ForeignHash(foreign_contract="__LEDGER__", foreign_name="balances")
        return hash_type.clear(foreign)
    if mode == "orm_foreign_clone":
        foreign = ForeignHash(foreign_contract="__LEDGER__", foreign_name="balances")
        return hash_type.clone_from(foreign, slot)
    if mode == "interface_noncallable":
        descriptor = importlib.Func("consume_payload")
        descriptor.is_of = 7
        module = importlib.import_module("__INSPECTOR__")
        return importlib.enforce_interface(module, [descriptor])
    raise Exception("unknown adapter mode")


@export
def event_owner_frame():
    before = [ctx.caller, ctx.signer, ctx.this]
    nested = inspector.event_frame()
    return save([before, nested, [ctx.caller, ctx.signer, ctx.this]])


@export
def forbidden_submit(name: str, unbound: bool):
    result.set("pending deployment must roll back")
    Attempted({"mode": "forbidden submit"})
    helper = Contract()
    source = "@export\ndef value():\n    return 7\n"
    if unbound:
        return Contract.submit(helper, name, source)
    submit = helper.submit
    return submit(name=name, code=source)


@export
def recover_submit(name: str, label: str):
    assert not importlib.exists(name), "forbidden submit persisted a child contract"
    return save(label)


@export
def constructor_submit(name: str, nested: str):
    result.set("pending constructor deployment must roll back")
    Attempted({"mode": "constructor submit"})
    source = r"""
marker = Variable()
Started = LogEvent("Started", {"name": str})
@construct
def seed(name: str):
    marker.set("pending child")
    Started({"name": name})
    Contract().submit(name, "@export\ndef value():\n    return 7\n")
@export
def value():
    return marker.get()
"""
    submission.submit_contract(name=name, code=source, constructor_args={"name": nested})


@export
def recover_constructor_submit(name: str, nested: str):
    assert not importlib.exists(name), "failed constructor persisted its child"
    assert not importlib.exists(nested), "failed constructor persisted its nested deployment"
    return save("recovered constructor submit")


@export
def imported_aliases():
    callback = inspector.frame_alias
    nested = callback()
    return save(
        [
            inspector.info_alias(ctx.this)["owner"],
            inspector.digest_alias("abc"),
            inspector.integer_alias("17"),
            inspector.fromhex_alias("6162").hex(),
            callback is inspector.event_frame,
            nested,
            [ctx.caller, ctx.signer, ctx.this],
        ]
    )


@export
def fail_imported_alias(mode: str):
    result.set("pending imported alias must roll back")
    Attempted({"mode": mode})
    if mode == "private":
        return inspector.private_alias(argument_failure())
    if mode == "storage":
        return inspector.storage_alias
    if mode == "storage_method":
        return inspector.storage_method_alias(argument_failure())
    if mode == "event":
        return inspector.event_alias(argument_failure())
    if mode == "constant":
        return inspector.constant_alias
    if mode == "namespace":
        return inspector.namespace_alias
    raise Exception("unknown imported alias mode")


subject_observations = []
subject_box = []


def observe_subject(subject):
    subject_observations.append(
        [
            subject.kind,
            subject.name,
            list(subject.args) if subject.args is not None else None,
            subject.storage_type,
            ctx.caller,
            ctx.signer,
            ctx.this,
        ]
    )
    if subject.kind == "function":
        matches = importlib.Func(subject.name, args=subject.args).is_of(subject)
        wrong = importlib.Var("value", Variable).is_of(subject)
    else:
        matches = importlib.Var("value", Variable).is_of(subject)
        wrong = importlib.Func("consume_payload", args=("payload",)).is_of(subject)
    return matches and not wrong


def reject_function_subject(subject):
    assert subject.kind == "function", "function callback received wrong metadata"
    return importlib.Func("consume_payload", args=("wrong",)).is_of(subject)


def reject_storage_subject(subject):
    assert subject.kind == "storage", "storage callback received wrong metadata"
    return importlib.Var("result", Hash).is_of(subject)


@export
def metadata_callbacks():
    function = importlib.Func("consume_payload", args=("payload",))
    variable = importlib.Var("result", Variable)
    private = importlib.Func("hidden_alias_target", private=True)
    function.is_of = observe_subject
    variable.is_of = observe_subject
    private.is_of = observe_subject
    module = importlib.import_module("__INSPECTOR__")
    accepted = importlib.enforce_interface(module, [function, variable, private])
    function.is_of = reject_function_subject
    rejected_function = importlib.enforce_interface(module, [function])
    variable.is_of = reject_storage_subject
    rejected_storage = importlib.enforce_interface(module, [variable])
    return save(
        [
            accepted,
            rejected_function,
            rejected_storage,
            subject_observations,
            [ctx.caller, ctx.signer, ctx.this],
        ]
    )


def capture_subject(subject):
    subject_box.append(subject)
    return True


def failing_subject_callback(subject):
    result.set("pending callback write must roll back")
    Attempted({"mode": "callback body"})
    assert subject.kind == "storage", "callback received wrong subject"
    raise Exception("metadata callback deliberate failure")


def interface_subject(storage):
    descriptor = (
        importlib.Var("result", Variable)
        if storage
        else importlib.Func("hidden_alias_target", private=True)
    )
    descriptor.is_of = capture_subject
    module = importlib.import_module("__INSPECTOR__")
    assert importlib.enforce_interface(module, [descriptor]), "metadata capture failed"
    return subject_box[0]


@export
def fail_metadata_callback(mode: str):
    result.set("pending metadata inspection must roll back")
    Attempted({"mode": mode})
    subject = interface_subject(mode in ["readonly", "storage_get", "storage_set", "public"])
    if mode == "readonly":
        subject.name = "changed"
    if mode == "private_call":
        return subject()
    if mode == "storage_get":
        return subject.get()
    if mode == "storage_set":
        return subject.set(argument_failure())
    if mode == "storage":
        return persisted.set({"subject": [subject]})
    if mode == "public":
        return {"subject": [subject]}
    if mode == "cross_contract":
        return inspector.consume_payload({"items": [subject]})
    if mode == "callback_error":
        descriptor = importlib.Var("result", Variable)
        descriptor.is_of = failing_subject_callback
        module = importlib.import_module("__INSPECTOR__")
        return importlib.enforce_interface(module, [descriptor])
    raise Exception("unknown metadata callback mode")


@export
def check_self_subject(subject: Any):
    result.set(
        [subject.kind, subject.name, list(subject.args), ctx.this, ctx.caller, ctx.owner, ctx.signer]
    )
    return importlib.Func("recover", args=("label",)).is_of(subject)


@export
def self_module_callback():
    before = [ctx.this, ctx.caller, ctx.owner, ctx.signer]
    module = importlib.import_module(ctx.this)
    descriptor = importlib.Func("recover", args=("label",))
    descriptor.is_of = module.check_self_subject
    accepted = importlib.enforce_interface(module, [descriptor])
    callback_frame = result.get()
    return save([before, accepted, callback_frame, [ctx.this, ctx.caller, ctx.owner, ctx.signer]])
