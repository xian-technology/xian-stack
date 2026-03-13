#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${script_dir}/stack-env.sh"

smoke_root="${XIAN_SMOKE_ROOT:-${stack_root}/.smoke}"
export XIAN_COMETBFT_HOME="${XIAN_COMETBFT_HOME:-${smoke_root}/cometbft}"
export XIAN_BDS_DATA_DIR="${XIAN_BDS_DATA_DIR:-${smoke_root}/bds}"
export_stack_env
require_stack_paths
require_docker
prepare_stack_dirs

smoke_moniker="${XIAN_SMOKE_MONIKER:-smoke-validator}"
smoke_genesis_file="${XIAN_SMOKE_GENESIS_FILE:-genesis-devnet.json}"
smoke_validator_privkey="${XIAN_SMOKE_VALIDATOR_PRIVKEY:-0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef}"
smoke_skip_build="${XIAN_SMOKE_SKIP_BUILD:-0}"
smoke_timeout_seconds="${XIAN_SMOKE_TIMEOUT_SECONDS:-90}"
smoke_status_url="${XIAN_SMOKE_STATUS_URL:-http://127.0.0.1:26657/status}"
smoke_abci_info_url="${XIAN_SMOKE_ABCI_INFO_URL:-http://127.0.0.1:26657/abci_info}"

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
    if docker compose -f docker-compose-abci.yml exec -T abci /bin/bash -lc \
      "python -c 'import requests, xian'" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done

  printf 'timed out waiting for abci container bootstrap\n' >&2
  docker compose -f docker-compose-abci.yml logs --tail=100 abci >&2 || true
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
make abci-up
wait_for_abci_runtime
make node-init
make node-id >/dev/null
make node-configure CONFIGURE_ARGS="--moniker ${smoke_moniker} --genesis-file-name ${smoke_genesis_file} --validator-privkey ${smoke_validator_privkey} --copy-genesis"

docker compose -f docker-compose-abci.yml exec -T abci /bin/bash -lc \
  "test -f /root/.cometbft/config/config.toml \
  && test -f /root/.cometbft/config/genesis.json \
  && test -f /root/.cometbft/config/priv_validator_key.json \
  && grep -q 'moniker = \"${smoke_moniker}\"' /root/.cometbft/config/config.toml"

make node-start
make --no-print-directory node-status >/tmp/xian-stack-node-status.json
wait_for_endpoint "${smoke_status_url}" "CometBFT RPC status"
wait_for_endpoint "${smoke_abci_info_url}" "ABCI info"

docker compose -f docker-compose-abci.yml exec -T abci /bin/bash -lc \
  "pm2 jlist | grep -q '\"name\":\"xian\"' && pm2 jlist | grep -q '\"name\":\"cometbft\"'"

python3 - <<'PY'
import json

with open("/tmp/xian-stack-node-status.json", "r", encoding="utf-8") as handle:
    payload = json.load(handle)

assert payload["abci_container_running"] is True
assert payload["required_processes_online"] is True
assert payload["backend_running"] is True
assert payload["node_id"]
PY

make node-stop
make abci-down

if [[ -n "$(docker compose -f docker-compose-abci.yml ps -q)" ]]; then
  printf 'abci stack is still running after shutdown\n' >&2
  exit 1
fi

trap - EXIT
printf 'xian-stack smoke test passed\n'
