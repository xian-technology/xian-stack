"""Independent transaction-context oracle for the public deterministic RNG stream."""

import calendar
import hashlib
import re
from datetime import datetime

_TIMESTAMP = re.compile(
    r"([0-9]{4}-[0-9]{2}-[0-9]{2}T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9])"
    r"(?:\.([0-9]{1,9}))?(Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)


def timestamp_nanoseconds(value):
    """Parse an RPC timestamp exactly, retaining its submicrosecond fraction."""
    match = _TIMESTAMP.fullmatch(value)
    if match is None:
        raise ValueError("invalid RFC3339 timestamp or nanosecond precision")
    seconds, fraction, offset = match.groups()
    instant = datetime.fromisoformat(seconds + offset)
    whole_seconds = calendar.timegm(instant.utctimetuple())
    return whole_seconds * 1_000_000_000 + int((fraction or "0").ljust(9, "0"))


def transaction_random_value(block, signature):
    header = block["block"]["header"]
    nanos = timestamp_nanoseconds(header["time"])
    input_hash = hashlib.sha3_256(f"{nanos}{signature}".encode()).hexdigest()
    seed = (
        f"chain_id={header['chain_id']}|block_num={header['height']}|"
        f"block_hash={block['block_id']['hash'].lower()}|input_hash={input_hash}|0"
    )
    return int.from_bytes(hashlib.sha3_256(seed.encode()).digest()[:8], "big")
