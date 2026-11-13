result = Variable()
fixed = Variable(t=decimal)
entries = Hash(default_value=0)
copy = Hash(default_value=0)
seed = 1


@export
def surface_boundaries(failure: str = ""):
    result.set("must roll back on failure")
    if failure == "classinfo":
        return isinstance(1, 1)
    if failure == "datetime":
        return datetime.datetime(2**32 + 2026, 1, 1)
    if failure == "format":
        return "{} {0}".format("x")
    parsed = datetime.datetime.strptime("23:45:01.12", "%H:%M:%S.%f")
    duration = datetime.timedelta(seconds=-1)
    frozen = frozenset([2, 1])
    output = [
        "¼".isdigit(),
        "\u0345".isalpha(),
        "a中".islower(),
        "\u001c x \u001c".strip(),
        "ΟΣ".lower(),
        "\u1c89".lower(),
        frozen.copy() is frozen,
        str(Exception("message")),
        isinstance(1, (int, abs)),
        issubclass(bool, bool),
        [parsed.year, parsed.month, parsed.day, parsed.hour, parsed.minute, parsed.microsecond],
        [duration.seconds, duration.minutes, duration.hours, duration.days, duration.weeks],
        str(duration),
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
    answer = [int("١_٢"), int("０xf", 0), int("١٢٠")]
    result.set(answer)
    return answer


@export
def exact_amounts():
    amount = 9007199254740993.012345678901234567890123456789
    values = [amount, (10**100) / (10**100), 1 / 3, -(1 / 3)]
    result.set(values)
    return values


@export
def settlement():
    values = [
        quantize(1.005, 2, "half_even"),
        quantize(-1.015, 2, "half_even"),
        quantize(-1.011, 2, "toward_zero"),
        quantize(-1.011, 2, "floor"),
        quantize(1.011, 2, "ceil"),
        mul_div_floor(-5, 1, 2),
        mul_div_ceil(-5, 1, 2),
        sqrt(2),
    ]
    result.set(values)
    return values


@export
def typed_failure():
    result.set("uncommitted")
    fixed.set("invalid")


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
    return decimal("0.0000000000000000000000000000001")
