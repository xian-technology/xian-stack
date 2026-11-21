#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${script_dir}/stack-env.sh"

export_stack_env
stack_root="$(cd "${script_dir}/.." && pwd)"
xian_cli_dir="${XIAN_CLI_DIR}"
smoke_root="${XIAN_SMOKE_CLI_ROOT:-${stack_root}/.smoke-cli}"
cli_workspace="${smoke_root}/workspace"
node_name="${XIAN_SMOKE_CLI_NODE_NAME:-smoke-validator}"

export XIAN_COMETBFT_HOME="${smoke_root}/cometbft"
export XIAN_BDS_DATA_DIR="${smoke_root}/bds"

prepare_stack_dirs
require_stack_paths
require_docker
require_uv

if [[ ! -d "${xian_cli_dir}" ]]; then
  printf 'missing xian-cli checkout: %s\n' "${xian_cli_dir}" >&2
  exit 1
fi

mkdir -p "${cli_workspace}"

cd "${stack_root}"

./scripts/validate-stack.sh
make abci-build
uv sync --project "${xian_cli_dir}" --group dev

uv run --project "${xian_cli_dir}" xian network join "${node_name}" \
  --base-dir "${cli_workspace}" \
  --network local \
  --node-image-mode local_build \
  --generate-validator-key \
  --stack-dir "${stack_root}" \
  --home "${XIAN_COMETBFT_HOME}"

# Persist the isolated host metrics binding so the profile cannot replace it
# with its ordinary 9108 default when the CLI calls the backend.
python3 - "${cli_workspace}/nodes/${node_name}.json" "${XIAN_APP_METRICS_PORT}" <<'PYTHON'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
profile = json.loads(path.read_text())
profile.setdefault("advanced", {}).setdefault("metrics", {}).update(
    host="127.0.0.1", port=int(sys.argv[2])
)
path.write_text(json.dumps(profile))
PYTHON

uv run --project "${xian_cli_dir}" xian node init "${node_name}" \
  --base-dir "${cli_workspace}" \
  --stack-dir "${stack_root}" \
  --home "${XIAN_COMETBFT_HOME}"

status_json="$(uv run --project "${xian_cli_dir}" xian node start "${node_name}" \
  --base-dir "${cli_workspace}" \
  --stack-dir "${stack_root}")"
printf '%s\n' "${status_json}" >"${smoke_root}/start.json"

python3 - "${smoke_root}/start.json" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    payload = json.load(handle)

assert payload["rpc_checked"] is True
assert payload["rpc_status"]["result"]["node_info"]["network"]
PY

status_json="$(uv run --project "${xian_cli_dir}" xian node status "${node_name}" \
  --rpc-url "http://127.0.0.1:${XIAN_COMETBFT_RPC_PORT}/status" \
  --base-dir "${cli_workspace}" \
  --stack-dir "${stack_root}")"
printf '%s\n' "${status_json}" >"${smoke_root}/status.json"

python3 - "${smoke_root}/status.json" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    payload = json.load(handle)

assert payload["backend_status"]["backend_running"] is True
assert payload["backend_status"]["required_processes_online"] is True
assert payload["rpc_reachable"] is True
PY

uv run --project "${xian_cli_dir}" xian node stop "${node_name}" \
  --base-dir "${cli_workspace}" \
  --stack-dir "${stack_root}" >"${smoke_root}/stop.json"

if [[ -n "$(docker compose --profile integrated -f docker-compose-abci.yml ps -q)" ]]; then
  printf 'abci stack is still running after CLI shutdown\n' >&2
  exit 1
fi

printf 'xian-stack CLI smoke test passed\n'
