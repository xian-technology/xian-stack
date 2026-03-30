#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${script_dir}/stack-env.sh"

smoke_root="${XIAN_SMOKE_ROOT:-${stack_root}/.smoke}"
export XIAN_COMETBFT_HOME="${XIAN_COMETBFT_HOME:-${smoke_root}/cometbft}"
export XIAN_BDS_DATA_DIR="${XIAN_BDS_DATA_DIR:-${smoke_root}/bds}"
export XIAN_COMETBFT_RPC_HOST="${XIAN_SMOKE_COMETBFT_RPC_HOST:-127.0.0.1}"
export XIAN_COMETBFT_RPC_PORT="${XIAN_SMOKE_COMETBFT_RPC_PORT:-28657}"
export XIAN_COMETBFT_P2P_HOST="${XIAN_SMOKE_COMETBFT_P2P_HOST:-127.0.0.1}"
export XIAN_COMETBFT_P2P_PORT="${XIAN_SMOKE_COMETBFT_P2P_PORT:-28656}"
export XIAN_COMETBFT_METRICS_HOST="${XIAN_SMOKE_COMETBFT_METRICS_HOST:-127.0.0.1}"
export XIAN_COMETBFT_METRICS_PORT="${XIAN_SMOKE_COMETBFT_METRICS_PORT:-28660}"
export XIAN_APP_METRICS_HOST="${XIAN_SMOKE_APP_METRICS_HOST:-127.0.0.1}"
export XIAN_APP_METRICS_PORT="${XIAN_SMOKE_APP_METRICS_PORT:-29108}"
export XIAN_DASHBOARD_HOST="${XIAN_SMOKE_DASHBOARD_HOST:-127.0.0.1}"
export XIAN_DASHBOARD_PORT="${XIAN_SMOKE_DASHBOARD_PORT:-28080}"
export_stack_env
prepare_stack_dirs
require_stack_paths
require_docker

smoke_moniker="${XIAN_SMOKE_MONIKER:-smoke-validator}"
smoke_genesis_source="${XIAN_SMOKE_GENESIS_SOURCE:-devnet}"
smoke_genesis_preset="${XIAN_SMOKE_GENESIS_PRESET:-}"
smoke_chain_id="${XIAN_SMOKE_CHAIN_ID:-}"
smoke_genesis_time="${XIAN_SMOKE_GENESIS_TIME:-}"
smoke_validator_privkey="${XIAN_SMOKE_VALIDATOR_PRIVKEY:-0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef}"
smoke_skip_build="${XIAN_SMOKE_SKIP_BUILD:-0}"
smoke_timeout_seconds="${XIAN_SMOKE_TIMEOUT_SECONDS:-90}"
smoke_status_url="${XIAN_SMOKE_STATUS_URL:-http://${XIAN_COMETBFT_RPC_HOST}:${XIAN_COMETBFT_RPC_PORT}/status}"
smoke_abci_info_url="${XIAN_SMOKE_ABCI_INFO_URL:-http://${XIAN_COMETBFT_RPC_HOST}:${XIAN_COMETBFT_RPC_PORT}/abci_info}"

if [[ -z "${smoke_genesis_preset}" ]]; then
  manifest_path="${XIAN_CONFIGS_DIR}/networks/${smoke_genesis_source}/manifest.json"
  if [[ -f "${manifest_path}" ]]; then
    smoke_genesis_preset="${smoke_genesis_source}"
    if [[ -z "${smoke_chain_id}" ]]; then
      smoke_chain_id="$(python3 - <<'PY' "${manifest_path}"
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    manifest = json.load(handle)

print(manifest["chain_id"])
PY
)"
    fi
    if [[ -z "${smoke_genesis_time}" ]]; then
      smoke_genesis_time="$(python3 - <<'PY' "${manifest_path}"
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    manifest = json.load(handle)

print(manifest.get("genesis_time") or "")
PY
)"
    fi
  fi
fi

wait_for_endpoint() {
  local url="$1"
  local description="$2"
  local deadline=$((SECONDS + smoke_timeout_seconds))

  while (( SECONDS < deadline )); do
    if curl -fsS "${url}" >/dev/null; then
      return 0
    fi
    sleep 2
  done

  printf 'timed out waiting for %s at %s\n' "${description}" "${url}" >&2
  return 1
}

wait_for_abci_runtime() {
  local deadline=$((SECONDS + smoke_timeout_seconds))

  while (( SECONDS < deadline )); do
    if docker compose --profile integrated -f docker-compose-abci.yml exec -T abci /bin/bash -lc \
      "python -c 'import contracting, xian'" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done

  printf 'timed out waiting for abci container bootstrap\n' >&2
  docker compose --profile integrated -f docker-compose-abci.yml logs --tail=100 abci >&2 || true
  return 1
}

cleanup() {
  set +e
  cd "${stack_root}" || return
  make node-stop >/dev/null 2>&1 || true
  make abci-down >/dev/null 2>&1 || true
}

trap cleanup EXIT

cd "${stack_root}"

./scripts/validate-stack.sh

if [[ "${smoke_skip_build}" != "1" ]]; then
  make abci-build
fi
make node-init
make node-id >/dev/null

configure_args=(
  --moniker "${smoke_moniker}"
  --validator-privkey "${smoke_validator_privkey}"
  --copy-genesis
)
if [[ -n "${smoke_genesis_preset}" ]]; then
  configure_args+=(--genesis-preset "${smoke_genesis_preset}")
  configure_args+=(--chain-id "${smoke_chain_id}")
  if [[ -n "${smoke_genesis_time}" ]]; then
    configure_args+=(--genesis-time "${smoke_genesis_time}")
  fi
else
  configure_args+=(--genesis-source "${smoke_genesis_source}")
fi

printf -v configure_args_quoted '%q ' "${configure_args[@]}"
make node-configure CONFIGURE_ARGS="${configure_args_quoted% }"

make node-start
wait_for_abci_runtime

make --no-print-directory node-status >/tmp/xian-stack-node-status.json
wait_for_endpoint "${smoke_status_url}" "CometBFT RPC status"
wait_for_endpoint "${smoke_abci_info_url}" "ABCI info"

python3 - <<'PY'
import json

with open("/tmp/xian-stack-node-status.json", "r", encoding="utf-8") as handle:
    payload = json.load(handle)

assert payload["runtime_services_running"] is True
assert payload["required_processes_online"] is True
assert payload["backend_running"] is True
assert payload["node_id"]
PY

make node-stop
make abci-down

if [[ -n "$(docker compose --profile integrated -f docker-compose-abci.yml ps -q)" ]]; then
  printf 'abci stack is still running after shutdown\n' >&2
  exit 1
fi

trap - EXIT
printf 'xian-stack smoke test passed\n'
