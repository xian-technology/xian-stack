"""Independent transaction-context oracle for the public deterministic RNG stream."""

import hashlib

from google.protobuf.timestamp_pb2 import Timestamp
from xian.utils.block import get_nanotime_from_block_time


def transaction_random_value(block, signature):
    header = block["block"]["header"]
    timestamp = Timestamp()
    timestamp.FromJsonString(header["time"])
    nanos = get_nanotime_from_block_time(timestamp)
    input_hash = hashlib.sha3_256(f"{nanos}{signature}".encode()).hexdigest()
    seed = (
        f"chain_id={header['chain_id']}|block_num={header['height']}|"
        f"block_hash={block['block_id']['hash'].lower()}|input_hash={input_hash}|0"
    )
    return int.from_bytes(hashlib.sha3_256(seed.encode()).digest()[:8], "big")
