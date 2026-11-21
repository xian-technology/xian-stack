#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${script_dir}/stack-env.sh"

export_stack_env

bds_enabled="${XIAN_BDS_ENABLED:-0}"
required_processes=("xian" "cometbft")

compose_cmd=(docker compose --profile integrated -f docker-compose-abci.yml)
if [[ "${bds_enabled}" == "1" || "${bds_enabled}" == "true" ]]; then
  compose_cmd=(docker compose --profile integrated -f docker-compose-abci.yml -f docker-compose-abci-bds.yml)
fi

compose_status_raw="[]"
runtime_status_known=0
if compose_status_raw="$("${compose_cmd[@]}" ps --all --format json 2>/dev/null)"; then
  runtime_status_known=1
else
  compose_status_raw="[]"
fi

inspect_runtime_processes() {
  local service_name="$1"

  "${compose_cmd[@]}" exec -T "${service_name}" python3 - <<'PY' 2>/dev/null || printf '[]'
import json
import subprocess

patterns = {
    "xian": "xian-abci",
    "cometbft": "cometbft node",
}

try:
    ps_output = subprocess.check_output(
        ["ps", "-eo", "pid=,comm=,args="],
        text=True,
    )
except Exception:
    print("[]")
    raise SystemExit(0)

items = []
for name, pattern in patterns.items():
    fallback = None
    preferred = None
    for raw_line in ps_output.splitlines():
        line = raw_line.strip()
        if not line or pattern not in line or "python3 - " in line:
            continue
        pid_text, command, args = line.split(None, 2)
        candidate = {
            "name": name,
            "pid": int(pid_text),
            "args": args,
            "running": True,
        }
        if fallback is None:
            fallback = candidate
        if command != "docker-init":
            preferred = candidate
            break
    items.append(
        preferred
        or fallback
        or {"name": name, "pid": None, "args": None, "running": False}
    )

print(json.dumps(items))
PY
}

runtime_processes_raw="[]"
node_id=""

if "${compose_cmd[@]}" exec -T abci /bin/bash -lc "true" >/dev/null 2>&1; then
  runtime_processes_raw="$(inspect_runtime_processes abci)"
  if node_id="$("${compose_cmd[@]}" exec -T abci cometbft show-node-id 2>/dev/null | tail -n 1)"; then
    node_id="${node_id//$'\r'/}"
  else
    node_id=""
  fi
fi

python3 - "${compose_status_raw}" "${runtime_processes_raw}" "${bds_enabled}" "${node_id}" "${runtime_status_known}" "${required_processes[@]}" <<'PY'
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
        "image": item.get("Image") or item.get("image"),
    }


compose_services = [
    normalize_compose_service(item) for item in parse_json_stream(sys.argv[1])
]
runtime_processes = parse_json_stream(sys.argv[2])
bds_enabled = sys.argv[3] in {"1", "true"}
node_id = sys.argv[4] or None
runtime_status_known = sys.argv[5] == "1"
required_processes = sys.argv[6:]

runtime_containers = ["abci"]

runtime_services_running = all(
    any(
        service["service"] == expected and service.get("state") == "running"
        for service in compose_services
    )
    for expected in runtime_containers
)

online_processes = {
    item["name"] for item in runtime_processes if item.get("running") is True
}
required_processes_online = all(
    process in online_processes for process in required_processes
)

result = {
    "bds_enabled": bds_enabled,
    "compose_services": compose_services,
    "runtime_containers": runtime_containers,
    "runtime_status_known": runtime_status_known,
    "runtime_services_running": runtime_services_running,
    "runtime_processes": runtime_processes,
    "required_processes": required_processes,
    "required_processes_online": required_processes_online,
    "backend_running": runtime_status_known and runtime_services_running and required_processes_online,
    "node_id": node_id,
}
print(json.dumps(result, indent=2))
PY
