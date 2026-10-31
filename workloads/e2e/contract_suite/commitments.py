used = Hash(default_value=False)
count = Variable()
result = Variable()
Accepted = LogEvent("Accepted", {"nonce": str, "signer": str})


@construct
def seed():
    count.set(0)


@export
def authorize(nonce: str, message: str, signature: str):
    assert not used[ctx.caller, nonce], "nonce already used"
    assert crypto.key_is_valid(ctx.caller), "invalid key"
    assert crypto.verify(ctx.caller, message, signature), "bad signature"
    used[ctx.caller, nonce] = True
    count.set(count.get() + 1)
    Accepted({"nonce": nonce, "signer": ctx.signer})
    return hashlib.sha3_text(message)


@export
def time_and_random():
    start = datetime.datetime.strptime("2024-02-28 12:00:00", "%Y-%m-%d %H:%M:%S")
    end = start + datetime.DAYS * 2
    assert end.day == 1 and end.month == 3 and end.year == 2024
    assert end - start == datetime.timedelta(days=2)
    assert now + datetime.SECONDS > now
    random.seed("contract-suite")
    numbers = [random.randint(1, 9), random.randrange(5) * 2, random.getrandbits(4)]
    assert 1 <= numbers[0] <= 9 and numbers[1] in [0, 2, 4, 6, 8] and 0 <= numbers[2] < 16
    assert 0 <= random.random() < 1
    values = [1, 2, 3]
    random.shuffle(values)
    assert sorted(values) == [1, 2, 3]
    assert random.choice(["x"]) == "x" and random.choices(["y"], 2) == ["y", "y"]
    answer = [end.year, end.month, end.day, len(values)]
    result.set(answer)
    return answer
