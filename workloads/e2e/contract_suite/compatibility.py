import __INSPECTOR__ as inspector

result = Variable()
fixed = Variable(t=decimal)
entries = Hash(default_value=0)
copy = Hash(default_value=0)
seed = 1


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
