result = Variable()


@export
def transform(text: str):
    parts = text.strip().lower().split(",")
    joined = "|".join(parts)
    assert joined.startswith("alpha") and joined.endswith("beta")
    assert joined.find("|") == 5 and joined.count("a") == 3
    assert joined.replace("|", ":").upper() == "ALPHA:BETA"
    assert "abc"[99::-1] == "cba" and "abc".find("", 4) == -1
    assert chr(65) == "A" and ord("A") == 65
    assert (bin(5), oct(8), hex(31), format(31, "04x")) == ("0b101", "0o10", "0x1f", "001f")
    assert ascii("A") == "'A'"
    data = bytearray(b"ABC")
    data.append(68)
    data[0] = 90
    assert bytes(data) == b"ZBCD" and bytes(data)[::-1] == b"DCBZ"
    assert bytes([65, 66]).hex() == "4142"
    assert bytes.fromhex("41 42") == b"AB"
    digest = hashlib.sha256_text(joined)
    assert hashlib.sha256("4142") == hashlib.sha256_hex("4142")
    assert hashlib.sha3("hello") == hashlib.sha3_text("hello")
    answer = [joined, digest, data.hex(), f"{len(parts)}:{parts[0]}"]
    result.set(answer)
    return answer
