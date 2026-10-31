result = Variable()


def twice(value):
    return value * 2


def positive(value):
    return value > 0


def combine(first, *rest, scale=2, **options):
    return (first + sum(rest)) * scale + options.get("offset", 0)


def factorial(value):
    if value <= 1:
        return 1
    return value * factorial(value - 1)


@export
def numbers():
    assert (7 // 3, -7 % 3, divmod(7, 3)) == (2, 2, (2, 1))
    assert abs(-3) + pow(2, 5) == 35
    assert round(float("2.5")) == 2 and int("0xff", base=0) == 255
    assert (5 & 3, 5 | 2, 5 ^ 3, ~5, 3 << 2, 12 >> 2) == (1, 7, 6, -6, 12, 3)
    assert bool([]) is False and not False and True + 1 == 2
    assert isinstance(True, int) and issubclass(bool, int)
    assert min([], default=9) == 9 and max([1, 3, 2]) == 3
    assert decimal("1") / decimal("8") == decimal("0.125")
    assert decimal("-3") // decimal("2") == -1
    value = [sum([1, 2], start=4), int(float("12.9")), round(125, -1)]
    result.set(value)
    return value


@export
def containers():
    shared = []
    mapping = dict.fromkeys(["a", "b"], shared)
    mapping["a"].append(1)
    assert mapping["a"] is mapping["b"] and mapping["b"] == [1]
    mapping["c"] = [3]
    assert mapping.get("c", []) == [3]
    assert mapping.pop("missing", 8) == 8
    copied = dict(mapping)
    copied["a"].extend([2])
    assert mapping["b"] == [1, 2]
    values = [3, 1, 2]
    values.sort(reverse=True)
    values.insert(1, 9)
    assert values.pop(1) == 9
    values.remove(2)
    values = list(reversed(values))
    assert values == [1, 3] and values.count(1) == 1 and values.index(3) == 1
    assert values[::-1] == [3, 1] and [0] * 2 + values == [0, 0, 1, 3]
    keys = set(["a", "b"])
    keys.add("c")
    keys.discard("missing")
    assert keys.intersection(set(["b", "d"])) == set(["b"])
    assert keys.difference(set(["a", "b"])) == set(["c"])
    assert frozenset(["a", "b"]).issubset(keys)
    answer = [sorted(keys), list(mapping.keys()), list(mapping.values())]
    result.set(answer)
    return answer


@export
def flow():
    selected = [i * i for i in range(6) if i % 2 == 0]
    lookup = {str(i): i * 2 for i in range(3)}
    total = 0
    for value in range(6):
        if value == 1:
            continue
        if value == 4:
            break
        total += value
    count = 0
    while count < 3:
        count += 1
    else:
        total += count
    for item in []:
        total = 999
    else:
        total += 1
    first, middle, last = [1, [2, 3], 4]
    assert first == 1 and middle == [2, 3] and last == 4
    assert combine(1, 2, 3, scale=3, offset=4) == 22
    assert factorial(5) == 120
    iterator = map(twice, filter(positive, [-1, 0, 1, 2]))
    assert tuple(iterator) == (2, 4) and list(iterator) == []
    assert list(zip([1, 2], reversed([3, 4]), strict=True)) == [(1, 4), (2, 3)]
    assert all([1, 2]) and any([0, 2])
    answer = [selected, lookup, total, "yes" if total == 9 else "no"]
    result.set(answer)
    return answer
