"""Canonical transaction-result comparison shared by RPC and replay capture."""

import hashlib
import json


def result_digest(results):
    normalized = []
    for result in results or []:
        normalized.append(
            {
                "code": int(result.get("code", 0)),
                "data": result.get("data") or "",
                "log": result.get("log") or "",
                "info": result.get("info") or "",
                "gas_wanted": int(result.get("gas_wanted", 0)),
                "gas_used": int(result.get("gas_used", 0)),
                "events": [
                    {
                        "type": event.get("type") or "",
                        "attributes": [
                            {
                                "key": attr.get("key") or "",
                                "value": attr.get("value") or "",
                                "index": bool(attr.get("index", False)),
                            }
                            for attr in event.get("attributes") or []
                        ],
                    }
                    for event in result.get("events") or []
                ],
            }
        )
    return hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
