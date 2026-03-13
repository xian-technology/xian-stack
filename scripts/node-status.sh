#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${script_dir}/stack-env.sh"

export_stack_env

compose_files=(-f docker-compose-abci.yml)
required_processes=("xian" "cometbft")
service_node="${XIAN_SERVICE_NODE:-0}"

if [[ "${service_node}" == "1" || "${service_node}" == "true" ]]; then
  compose_files+=(-f docker-compose-abci-bds.yml)
  required_processes+=("simulator")
fi

compose_cmd=(docker compose "${compose_files[@]}")
compose_status_raw="[]"
if compose_status_raw="$("${compose_cmd[@]}" ps --format json 2>/dev/null)"; then
  :
fi

pm2_raw="[]"
node_id=""
if "${compose_cmd[@]}" exec -T abci /bin/bash -lc "true" >/dev/null 2>&1; then
  if pm2_raw="$("${compose_cmd[@]}" exec -T abci /bin/bash -lc "pm2 jlist" 2>/dev/null)"; then
    :
  fi
  if node_id="$("${compose_cmd[@]}" exec -T abci /bin/bash -lc "cd /usr/src/app/xian-abci && make node-id" 2>/dev/null | tail -n 1)"; then
    node_id="${node_id//$'\r'/}"
  else
    node_id=""
  fi
fi

python3 - "${compose_status_raw}" "${pm2_raw}" "${service_node}" "${node_id}" "${required_processes[@]}" <<'PY'
import json
import sys


def parse_json_stream(raw: str):
    raw = raw.strip()
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        items = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            items.append(json.loads(line))
        return items
    if isinstance(payload, list):
        return payload
    return [payload]


def normalize_compose_service(item: dict) -> dict:
    return {
        "service": item.get("Service") or item.get("Name") or item.get("service"),
        "state": item.get("State") or item.get("state"),
        "status": item.get("Status") or item.get("status"),
        "health": item.get("Health") or item.get("health"),
        "exit_code": item.get("ExitCode") or item.get("exit_code"),
    }


compose_services = [
    normalize_compose_service(item) for item in parse_json_stream(sys.argv[1])
]
pm2_processes = []
for item in parse_json_stream(sys.argv[2]):
    name = item.get("name")
    status = item.get("pm2_env", {}).get("status")
    if name is None:
        continue
    pm2_processes.append({"name": name, "status": status})

service_node = sys.argv[3] in {"1", "true"}
node_id = sys.argv[4] or None
required_processes = sys.argv[5:]
abci_service = next(
    (item for item in compose_services if item["service"] == "abci"),
    None,
)
abci_container_running = (
    abci_service is not None and abci_service.get("state") == "running"
)
online_processes = {
    item["name"]
    for item in pm2_processes
    if item.get("status") == "online"
}
required_processes_online = all(
    process in online_processes for process in required_processes
)

result = {
    "service_node": service_node,
    "compose_services": compose_services,
    "abci_container_running": abci_container_running,
    "pm2_processes": pm2_processes,
    "required_processes": required_processes,
    "required_processes_online": required_processes_online,
    "backend_running": abci_container_running and required_processes_online,
    "node_id": node_id,
}
print(json.dumps(result, indent=2))
PY
