"""Record actual re-execution outputs, independently of CometBFT's stored responses."""

import json
from pathlib import Path

from google.protobuf.json_format import MessageToDict
from localnet_replay_capture import result_digest
from xian.xian_abci import Xian

original_finalize = Xian.finalize_block


async def capture(self, req):
    response = await original_finalize(self, req)
    result = MessageToDict(response, preserving_proto_field_name=True)
    path = Path("/root/.cometbft/replay-results.jsonl")
    with path.open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "height": req.height,
                    "app_hash": response.app_hash.hex(),
                    "digest": result_digest(result.get("tx_results", [])),
                }
            )
            + "\n"
        )
    return response


Xian.finalize_block = capture
