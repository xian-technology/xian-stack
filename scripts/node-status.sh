#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${script_dir}/stack-env.sh"

export_stack_env

stack_topology="${XIAN_STACK_TOPOLOGY:-integrated}"
service_node="${XIAN_SERVICE_NODE:-0}"
required_processes=("xian" "cometbft")

if [[ "${stack_topology}" == "integrated" ]]; then
  compose_files=(docker-compose-abci.yml)
  compose_cmd=(docker compose --profile integrated -f docker-compose-abci.yml)
  runtime_services=("abci")
  if [[ "${service_node}" == "1" || "${service_node}" == "true" ]]; then
    compose_files+=(docker-compose-abci-bds.yml)
    compose_cmd=(docker compose --profile integrated -f docker-compose-abci.yml -f docker-compose-abci-bds.yml)
  fi
elif [[ "${stack_topology}" == "fidelity" ]]; then
  compose_files=(docker-compose-abci.yml)
  compose_cmd=(docker compose --profile fidelity -f docker-compose-abci.yml)
  runtime_services=("abci-app" "cometbft")
else
  printf 'unsupported XIAN_STACK_TOPOLOGY: %s\n' "${stack_topology}" >&2
  exit 1
fi

compose_status_raw="[]"
if compose_status_raw="$("${compose_cmd[@]}" ps --format json 2>/dev/null)"; then
  :
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

if [[ "${stack_topology}" == "integrated" ]]; then
  if "${compose_cmd[@]}" exec -T abci /bin/bash -lc "true" >/dev/null 2>&1; then
    runtime_processes_raw="$(inspect_runtime_processes abci)"
    if node_id="$("${compose_cmd[@]}" exec -T abci cometbft show-node-id 2>/dev/null | tail -n 1)"; then
      node_id="${node_id//$'\r'/}"
    else
      node_id=""
    fi
  fi
else
  abci_processes="[]"
  cometbft_processes="[]"
  if "${compose_cmd[@]}" exec -T abci-app /bin/bash -lc "true" >/dev/null 2>&1; then
    abci_processes="$(inspect_runtime_processes abci-app)"
  fi
  if "${compose_cmd[@]}" exec -T cometbft /bin/bash -lc "true" >/dev/null 2>&1; then
    cometbft_processes="$(inspect_runtime_processes cometbft)"
    if node_id="$("${compose_cmd[@]}" exec -T cometbft cometbft show-node-id 2>/dev/null | tail -n 1)"; then
      node_id="${node_id//$'\r'/}"
    else
      node_id=""
    fi
  fi
  runtime_processes_raw="$(python3 - "${abci_processes}" "${cometbft_processes}" <<'PY'
import json
import sys

merged = {}
for raw in sys.argv[1:]:
    for item in json.loads(raw):
        current = merged.get(item["name"])
        if current is None or item.get("running"):
            merged[item["name"]] = item

for name in ("xian", "cometbft"):
    merged.setdefault(
        name,
        {"name": name, "pid": None, "args": None, "running": False},
    )

print(json.dumps(list(merged.values())))
PY
)"
fi

python3 - "${compose_status_raw}" "${runtime_processes_raw}" "${stack_topology}" "${service_node}" "${node_id}" "${required_processes[@]}" <<'PY'
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
stack_topology = sys.argv[3]
service_node = sys.argv[4] in {"1", "true"}
node_id = sys.argv[5] or None
required_processes = sys.argv[6:]

runtime_containers = {
    "integrated": ["abci"],
    "fidelity": ["abci-app", "cometbft"],
}.get(stack_topology, [])

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
    "topology": stack_topology,
    "service_node": service_node,
    "compose_services": compose_services,
    "runtime_containers": runtime_containers,
    "runtime_services_running": runtime_services_running,
    "runtime_processes": runtime_processes,
    "required_processes": required_processes,
    "required_processes_online": required_processes_online,
    "backend_running": runtime_services_running and required_processes_online,
    "node_id": node_id,
}
print(json.dumps(result, indent=2))
PY
