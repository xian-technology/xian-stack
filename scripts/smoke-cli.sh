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
smoke_skip_build="${XIAN_SMOKE_SKIP_BUILD:-0}"

export XIAN_COMETBFT_HOME="${smoke_root}/cometbft"
export XIAN_BDS_DATA_DIR="${smoke_root}/bds"

cleanup() {
  set +e
  cd "${stack_root}" || return
  uv run --project "${xian_cli_dir}" xian node stop "${node_name}" --base-dir "${cli_workspace}" >/dev/null 2>&1 || true
  make abci-down >/dev/null 2>&1 || true
}

trap cleanup EXIT

prepare_stack_dirs
require_stack_paths
require_docker
require_uv

if [[ ! -d "${xian_cli_dir}" ]]; then
  printf 'missing xian-cli checkout: %s\n' "${xian_cli_dir}" >&2
  exit 1
fi

rm -rf "${smoke_root}"
mkdir -p "${cli_workspace}"

cd "${stack_root}"

./scripts/validate-stack.sh
if [[ "${smoke_skip_build}" != "1" ]]; then
  make abci-build
fi
uv sync --project "${xian_cli_dir}" --group dev

uv run --project "${xian_cli_dir}" xian network join "${node_name}" \
  --base-dir "${cli_workspace}" \
  --network devnet \
  --generate-validator-key \
  --stack-dir "${stack_root}" \
  --home "${XIAN_COMETBFT_HOME}"

uv run --project "${xian_cli_dir}" xian node init "${node_name}" \
  --base-dir "${cli_workspace}" \
  --stack-dir "${stack_root}" \
  --home "${XIAN_COMETBFT_HOME}"

status_json="$(uv run --project "${xian_cli_dir}" xian node start "${node_name}" \
  --base-dir "${cli_workspace}" \
  --stack-dir "${stack_root}")"
printf '%s\n' "${status_json}" >/tmp/xian-stack-smoke-cli-start.json

python3 - <<'PY'
import json

with open("/tmp/xian-stack-smoke-cli-start.json", "r", encoding="utf-8") as handle:
    payload = json.load(handle)

assert payload["rpc_checked"] is True
assert payload["rpc_status"]["result"]["node_info"]["network"]
PY

status_json="$(uv run --project "${xian_cli_dir}" xian node status "${node_name}" \
  --base-dir "${cli_workspace}" \
  --stack-dir "${stack_root}")"
printf '%s\n' "${status_json}" >/tmp/xian-stack-smoke-cli-status.json

python3 - <<'PY'
import json

with open("/tmp/xian-stack-smoke-cli-status.json", "r", encoding="utf-8") as handle:
    payload = json.load(handle)

assert payload["backend_status"]["backend_running"] is True
assert payload["backend_status"]["required_processes_online"] is True
assert payload["rpc_reachable"] is True
PY

uv run --project "${xian_cli_dir}" xian node stop "${node_name}" \
  --base-dir "${cli_workspace}" \
  --stack-dir "${stack_root}" >/tmp/xian-stack-smoke-cli-stop.json

if [[ -n "$(docker compose -f docker-compose-abci.yml ps -q)" ]]; then
  printf 'abci stack is still running after CLI shutdown\n' >&2
  exit 1
fi

trap - EXIT
printf 'xian-stack CLI smoke test passed\n'
