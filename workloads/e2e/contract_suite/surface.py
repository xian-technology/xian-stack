import __INSPECTOR__ as inspector

result = Variable()
converted_members = Variable()
Attempted = LogEvent("Attempted", {"mode": str})


def save(value):
    result.set(value)
    return value


@export
def callables():
    values = [3, 1]
    append = values.append
    order = values.sort
    values.extend([2])
    append(4)
    order(reverse=True)
    split = "a,b,c".split
    render = "item={:02d}".format
    shared = [1]
    returned = inspector.consume_payload(payload={"items": shared})
    return save(
        [
            values,
            split(sep=",", maxsplit=1),
            list(map(render, [3, 1])),
            [shared, returned, shared is returned],
        ]
    )


@export
def numerics():
    number = 257
    encoded = number.to_bytes(length=2, byteorder="big", signed=False)
    interval = range(10, -1, -3)
    binary = float("1.5")
    return save(
        [
            number.bit_length(),
            number.bit_count(),
            encoded.hex(),
            int.from_bytes(encoded, byteorder="big", signed=False),
            [interval.start, interval.stop, interval.step, interval.count(4), interval.index(1)],
            list(binary.as_integer_ratio()),
            binary.hex(),
            float.fromhex(binary.hex()) == binary,
            [number.real, number.imag, number.numerator, number.denominator],
            [True.bit_length(), True.is_integer()],
        ]
    )


@export
def mappings():
    entries = {"a": 1, "b": 2}
    keys = entries.keys()
    items = entries.items()
    view = keys.mapping
    last = entries.popitem()
    entries["c"] = 3
    return save(
        [
            list(last),
            list(view),
            view["c"],
            sorted(keys & set(["a", "z"])),
            sorted(keys | set(["z"])),
            keys.isdisjoint(["x"]),
            items.isdisjoint([("b", 2)]),
            [list(pair) for pair in sorted(items & set([("a", 1), ("b", 2)]))],
            list(Exception("problem", 7).args),
        ]
    )


@export
def text():
    table = str.maketrans("ab", "XY", "!")
    return save(
        [
            "Straße".casefold(),
            "ǳABC".capitalize(),
            "a\tb".expandtabs(4),
            "prefix-value-suffix".removeprefix("prefix-").removesuffix("-suffix"),
            list("left:right:end".rpartition(":")),
            "a,b,c".rsplit(",", 1),
            " a\nb\r\nc ".splitlines(),
            "ab!cab".translate(table),
            ["²".isdecimal(), "²".isnumeric(), "name_2".isidentifier(), "\u001c".isspace()],
            "-42".zfill(6),
            "xy".center(6, "."),
            "ΟΣ".title(),
        ]
    )


@export
def binary():
    data = bytearray(b"abc")
    alias = data
    insert = data.insert
    insert(1, 120)
    data.remove(98)
    data.reverse()
    encoded = "café".encode(encoding="utf-8")
    return save(
        [
            alias.hex(),
            alias is data,
            b"a,b,c".rsplit(b",", 1)[1].decode("ascii"),
            [part.hex() for part in b"left:right".partition(b":")],
            b"-".join([b"a", b"b"]).hex(),
            b"\x01\x02\x03".hex(sep=":", bytes_per_sep=1),
            encoded.hex(),
            encoded.decode(encoding="utf-8"),
            "é".encode("latin-1").hex(),
            b"\xff".decode("ascii", errors="replace"),
            b" ab ".strip().upper().hex(),
            b"abc".count(b"b"),
            bytearray(b"").fromhex("41 42").hex(),
        ]
    )


@export
def time_policy():
    two = datetime.timedelta(days=2)
    three = datetime.timedelta(days=3)
    mixed = datetime.timedelta(days=1, seconds=2) * datetime.timedelta(days=2, seconds=3)
    negative = datetime.timedelta(seconds=-1) * datetime.timedelta(seconds=-1)
    iso = datetime.datetime.strptime("2024-W01-1", "%G-W%V-%u")
    week = datetime.datetime.strptime("2026 00 0", "%Y %U %w")
    return save(
        [
            (two * three).seconds,
            mixed.seconds,
            negative.seconds,
            [iso.year, iso.month, iso.day],
            [week.year, week.month, week.day],
            str(datetime.timedelta(seconds=-1)),
        ]
    )


@export
def formatting():
    render = "{name}: {amount:04d} {label!r}".format
    name = "alice"
    amount = 7
    width = 4
    return save(
        [
            render(name="alice", amount=7, label="ok"),
            "{name}/{value}".format_map({"name": "x", "value": 3}),
            "{0[1]}:{1.real}".format(["a", "b"], 7),
            "{value:>{width}}".format(value="x", width=4),
            f"{name!r}:{amount:0{width}d}",
        ]
    )


@export
def fail(mode: str):
    result.set("pending write must roll back")
    Attempted({"mode": mode})
    if mode == "bound_storage":
        return result.set([].append)
    if mode == "keyword":
        return "a,b".split(unknown=True)
    if mode == "time_extension":
        return datetime.datetime.strptime("2026-01-01", "%F")
    if mode == "time_duplicate":
        return datetime.datetime.strptime("2026 2026", "%Y %Y")
    if mode == "decode":
        return b"\xff".decode("utf-8")
    if mode == "format":
        return "{0!q}".format(1)
    if mode == "encoding":
        return "hello".encode("utf-16")
    raise Exception("unknown failure mode")


@export
def recover(label: str):
    return save(label)


@export
def set_kinds():
    entries = {"a": 1, "b": 2}
    keys = entries.keys()
    raw = keys | []
    wrapped = set(["a", "b"])
    comparisons = [
        raw == wrapped,
        wrapped == raw,
        raw == keys,
        isinstance(raw, set),
        isinstance(raw, frozenset),
    ]
    methods = [
        isinstance(raw.copy(), set),
        isinstance(wrapped.union(raw), set),
        isinstance(raw.union(wrapped), set),
    ]
    mutable = keys | []
    alias = mutable
    mutable |= {"c": 3}.keys() | []
    same_kind = [sorted(alias), mutable is alias]
    mutable |= {"d": 4}.keys()
    view_operand = [sorted(alias), sorted(mutable), mutable is alias]
    returned = inspector.retain_builtin_set(value=raw)
    converted = set(returned)
    assert isinstance(converted, set)
    converted_members.set(sorted(converted))
    return save(
        [
            comparisons,
            methods,
            same_kind,
            view_operand,
            [sorted(raw), returned is raw, isinstance(returned, set)],
        ]
    )


@export
def read_converted_set():
    value = set(converted_members.get())
    return save([sorted(value), isinstance(value, set)])


@export
def recover_set(label: str):
    value = set(converted_members.get())
    assert isinstance(value, set) and sorted(value) == ["a", "b", "z"]
    return save(label)


@export
def host_aliases(target: str):
    module = hashlib
    callbacks = {"digest": module.sha3}
    decimal_value = decimal
    date = datetime.datetime
    duration = datetime.timedelta
    parse = date(2026, 1, 2).strptime
    exists = importlib.exists
    has_export = importlib.has_export
    interface_function = importlib.Func
    check_interface = importlib.enforce_interface
    seed = random.seed
    choose = random.choice
    shuffle = random.shuffle
    seed("host-aliases")
    values = [[7]]
    selected = choose(values)
    selected.append(8)
    shuffle(values)
    return save(
        [
            list(map(callbacks["digest"], ["abc", ""])),
            [str(value) for value in map(decimal_value, ["1.25", "2.5"])],
            isinstance(decimal_value("2"), decimal_value),
            str(date(2026, 1, 2) + duration(days=2)),
            [
                isinstance(parse("2024-01-02", "%Y-%m-%d"), date),
                parse == date.strptime,
                isinstance(list(map(parse, ["2024-01-02"], ["%Y-%m-%d"]))[0], date),
            ],
            [
                exists(target),
                has_export(target, "consume_payload"),
                check_interface(target, [interface_function("consume_payload", args=("payload",))]),
                not check_interface(
                    target, [interface_function("consume_payload", args=("wrong",))]
                ),
            ],
            [values, selected is values[0]],
        ]
    )


@export
def fail_host_alias(mode: str):
    result.set("host callable pending write must roll back")
    Attempted({"mode": mode})
    if mode == "constructor_storage":
        constructor = decimal
        return result.set({"nested": [constructor]})
    if mode == "constructor_result":
        constructor = datetime.datetime
        return {"nested": [constructor]}
    digest = hashlib.sha3
    if mode == "host_storage":
        return result.set({"nested": [digest]})
    if mode == "host_result":
        return {"nested": [digest]}
    assert mode == "host_cross", "unknown host callable failure mode"
    return inspector.consume_payload(payload={"callback": [digest]})


@export
def fail_set(mode: str):
    result.set("set pending write must roll back")
    Attempted({"mode": mode})
    value = {"a": 1}.keys() | []
    if mode == "set_storage":
        return converted_members.set({"nested": [value]})
    if mode == "set_result":
        return {"nested": [value]}
    if mode == "set_mixed_left":
        return sorted(value | set(["b"]))
    assert mode == "set_mixed_right", "unknown set failure mode"
    return sorted(set(["b"]) | value)


@export
def fail_cross_contract(mode: str):
    result.set("cross-contract pending write must roll back")
    Attempted({"mode": mode})
    if mode == "cross_inject":
        items = []
        inspector.inject_capability(values=items)
        return 7
    payload = {"items": [1]}
    if mode == "cross_bound":
        payload["callback"] = [result.get]
    else:
        assert mode == "cross_proxy", "unknown cross-contract mode"
        entries = {"x": 7}
        payload["mapping"] = [entries.keys().mapping]
    return inspector.consume_payload(payload=payload)
