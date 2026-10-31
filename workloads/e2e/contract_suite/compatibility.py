import __INSPECTOR__ as inspector

result = Variable()
fixed = Variable(t=decimal)
entries = Hash(default_value=0)
copy = Hash(default_value=0)
seed = 1


@export
def float_sets():
    a = float("nan")
    b = float("-nan")
    positive_zero = float("0")
    negative_zero = float("-0")
    values = [float(x) for x in ["10", "2", "-2", "-10", "inf", "-inf"]]
    numbers = [str(x) for x in set(values)]
    nested = [str(x[0]) for x in set([(x,) for x in values])]
    frozen = [str(list(x)[0]) for x in set([frozenset([x]) for x in values])]
    special = set([a, float("2"), b, float("10"), a])
    popped = special.pop()
    special.add(float("1"))
    ordered = list(special.copy())
    output = [
        numbers,
        nested,
        frozen,
        str(list(set([positive_zero, negative_zero]))[0]),
        str(list(set([negative_zero, positive_zero]))[0]),
        str(popped),
        [str(x) for x in ordered],
        ordered[2] is a,
        ordered[3] is b,
        a in special,
        float("nan") in special,
        a == a,
    ]
    result.set(output)
    return output


def first(value=seed):
    return value


seed = 2


def second(value=seed):
    return value


seed = 3
streamed = {}


def pair(index):
    if index == 1:
        assert streamed["first"] == 1
    return [("first", 1), ("second", 2)][index]


def combine(first, *rest, scale=1, **options):
    return (first + sum(rest)) * scale + options.get("offset", 0)


@export
def bindings():
    answer = [first(), second(), seed]
    result.set(answer)
    return answer


@export
def unpacking():
    start, *middle, end = range(5)
    shared = []
    expanded = [*([shared],), *[shared]]
    expanded[1].append(9)
    assert expanded[0][0] is shared and expanded[0][0] == [9]
    assert [(a, b) for a, *b in [(1, 2, 3), (4,)]] == [(1, [2, 3]), (4, [])]
    answer = [start, middle, end, combine(*[1, 2], *[3], **{"scale": 2}, offset=4)]
    result.set(answer)
    return answer


@export
def methods():
    streamed.update(map(pair, range(2)))
    shared = []
    assert streamed.setdefault("shared", shared) is shared
    shared.append(3)
    values = [1, 2, 3]
    alias = values
    assert values.reverse() is None and alias == [3, 2, 1]
    assert bytes.fromhex("41 42\t43") == b"ABC"
    answer = [list(streamed.keys()), streamed["shared"], alias]
    result.set(answer)
    return answer


@export
def mutation(visited: bool):
    mapping = {"a": 1, "b": 2}
    answer = []
    result.set("uncommitted")
    for key in mapping:
        answer.append(key)
        if key == "a":
            mapping.pop("a" if visited else "b")
            mapping["c"] = 3
    result.set(answer)
    return answer


@export
def compaction():
    mapping = dict(zip("abcde", range(5)))
    answer = []
    for key in mapping:
        answer.append(key)
        if key == "a":
            mapping.pop("b")
            mapping["z"] = 9
    result.set(answer)
    return answer


@export
def unicode_numbers():
    answer = [int("١_٢"), int("０xf", 0), float("١.٢e٢")]
    result.set(answer)
    return answer


@export
def quantum():
    raw = round(decimal("2"), 2)
    negative = round(decimal("-0.001"), 2)
    assert not isinstance(raw, decimal)
    result.set(raw)
    assert isinstance(result.get(), decimal) and str(result.get()) == "2"
    fixed.set(decimal(raw))
    answer = [
        raw,
        negative,
        str(raw),
        format(raw, ".3f"),
        str(raw + raw),
        inspector.echo_value(value=raw),
    ]
    entries["quantum"] = str(negative)
    return answer


@export
def typed_failure():
    result.set("uncommitted")
    fixed.set(round(decimal("2"), 2))


@export
def bulk():
    entries["a"] = 9
    entries["a", "x"] = 1
    entries["b", "y"] = 2
    copy.clone_from(entries)
    entries.clear("a")
    copy.clone_from(copy)
    answer = [
        entries["a"],
        entries["a", "x"],
        entries["b", "y"],
        [copy["a"], copy["a", "x"], copy["b", "y"]],
    ]
    result.set(answer)
    return answer


@export
def bulk_failure():
    entries.clear()
    copy.clear()
    result.set("uncommitted")
    assert False, "bulk deliberate"


@export
def malformed(kind: str):
    result.set("uncommitted")
    if kind == "hex":
        return bytes.fromhex("4 1")
    if kind == "unpack":
        a, *b, c = [1]
        return [a, b, c]
    return round(decimal("1"), 1000000)


# R17: raw Decimal results are observed before crossing finite storage boundaries.


@export
def decimal_classification():
    x = round(decimal("2.500"), 3)
    y = round(decimal("1.20"), 2)
    assert x.canonical() is x and x.conjugate() is x and x.real is x
    assert x.copy_abs() is not x and x.copy_negate() is not x
    assert str(x.imag) == "0"
    answer = [
        str(x.is_canonical()),
        str(x.is_finite()),
        str(x.is_infinite()),
        str(x.is_nan()),
        str(x.is_normal()),
        str(x.is_qnan()),
        str(x.is_signed()),
        str(x.is_snan()),
        str(x.is_subnormal()),
        str(x.is_zero()),
        str(x.number_class()),
        str(x.canonical()),
        str(x.conjugate()),
        str(x.copy_abs()),
        str(x.copy_negate()),
        str(x.copy_sign(-y)),
    ]
    result.set(answer)
    return answer


@export
def decimal_quantization():
    x = round(decimal("2.500"), 3)
    y = round(decimal("1.20"), 2)
    amount = round(decimal("2.555"), 3)
    cent = round(decimal("0.01"), 2)
    assert str(amount.quantize(cent, "ROUND_UP")) == "2.56"
    assert str(amount.quantize(cent, rounding="ROUND_HALF_EVEN")) == "2.56"
    assert str(amount.quantize(cent)) == "2.55"
    answer = [
        str(x.normalize()),
        str(x.quantize(y)),
        str(x.same_quantum(y)),
        str(x.to_eng_string()),
        str(x.to_integral()),
        str(x.to_integral_exact()),
        str(x.to_integral_value()),
    ]
    result.set(answer)
    return answer


@export
def decimal_math():
    x = round(decimal("2.500"), 3)
    y = round(decimal("1.20"), 2)
    answer = [
        str(x.adjusted()),
        str(x.exp()),
        str(x.fma(y, y)),
        str(x.ln()),
        str(x.log10()),
        str(x.logb()),
        str(x.sqrt()),
        str(x.radix()),
        str(x.remainder_near(y)),
    ]
    result.set(answer)
    return answer


@export
def decimal_ordering():
    x = round(decimal("2.500"), 3)
    y = round(decimal("1.20"), 2)
    answer = [
        str(x.compare(y)),
        str(x.compare_signal(y)),
        str(x.compare_total(y)),
        str(x.compare_total_mag(-y)),
        str(x.max(y)),
        str(x.max_mag(-y)),
        str(x.min(y)),
        str(x.min_mag(-y)),
        str(x.next_minus()),
        str(x.next_plus()),
        str(x.next_toward(y)),
        str(x.rotate(1)),
        str(x.scaleb(2)),
        str(x.shift(1)),
    ]
    result.set(answer)
    return answer


@export
def decimal_logical():
    x = round(decimal("101"), 0)
    y = round(decimal("11"), 0)
    answer = [
        str(x.logical_and(y)),
        str(x.logical_or(y)),
        str(x.logical_xor(y)),
        str(x.logical_invert()),
    ]
    result.set(answer)
    return answer


@export
def decimal_powers():
    base = round(decimal("2"), 2)
    power = round(decimal("1.2"), 1)
    answer = [str(base**power), str(base**-power)]
    result.set(answer)
    return answer


@export
def decimal_negative_power():
    result.set("uncommitted")
    return round(decimal("-2"), 2) ** round(decimal("1.2"), 1)


@export
def decimal_tuple_exact():
    raw = round(decimal("2.500"), 3)
    parts = raw.as_tuple()
    assert isinstance(parts, tuple) and len(parts) == 3
    assert parts[0] == parts.sign and parts[1] == parts.digits and parts[2] == parts.exponent
    assert list(parts) == [parts.sign, parts.digits, parts.exponent]
    assert raw.as_integer_ratio() == (5, 2)
    exact = raw.from_float(float("0.1"))
    assert exact != round(exact, 17)
    tiny = raw.from_float(float("5e-324")).as_tuple()
    assert len(tiny.digits) == 751 and tiny.exponent == -1074
    assert str(raw.from_float(10**150)) == "1" + "0" * 150
    answer = [parts.sign, list(parts.digits), parts.exponent, str(exact)]
    result.set(answer)
    return answer


@export
def decimal_nonfinite():
    raw = round(decimal("0"), 2)
    infinity = raw**-1
    echoed = inspector.echo_value(value=infinity)
    assert str(echoed[0]) == "Infinity" and echoed[1] == "Infinity" and not echoed[2]
    nan = raw.from_float(float("nan"))
    assert nan.as_tuple().exponent == "n" and infinity.as_tuple().exponent == "F"
    answer = [
        str(infinity),
        str(-infinity),
        str(infinity + raw),
        bool(infinity),
        infinity.is_infinite(),
        infinity.is_finite(),
        str(nan),
        nan.is_nan(),
        nan.is_qnan(),
    ]
    result.set(answer)
    return answer


@export
def decimal_nan_identity():
    raw = round(decimal("0"), 2)
    nan = raw.from_float(float("nan"))
    other = raw.from_float(float("nan"))
    assert nan.canonical() is nan and nan.conjugate() is nan and nan.real is nan
    assert nan.copy_abs() is not nan
    answer = [
        nan == nan,
        nan != nan,
        nan in [nan],
        [nan] == [nan],
        nan in {nan: 1},
        nan in set([nan]),
        len({nan: 1, other: 2}),
        len(set([nan, other])),
        other is nan,
    ]
    result.set(answer)
    return answer


@export
def decimal_nonfinite_storage(kind: str, target: str, nested: bool):
    raw = round(decimal("0"), 2)
    value = raw**-1 if kind == "infinity" else raw.from_float(float("nan"))
    result.set("uncommitted")
    entries["recovery"] = "uncommitted"
    if nested:
        value = {"payload": [value]}
    if target == "variable":
        result.set(value)
    else:
        entries["nonfinite"] = value


@export
def decimal_recovery(marker: str):
    assert result.get() != "uncommitted" and entries["recovery"] != "uncommitted"
    assert entries["nonfinite"] == 0
    result.set(marker)
    entries["recovery"] = marker
    return marker
