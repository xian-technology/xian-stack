#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/stack-env.sh"

export_stack_env
prepare_stack_dirs
require_stack_paths
require_docker
require_uv

cd "${stack_root}"

python3 ./scripts/release_manifest.py validate
docker compose --profile integrated -f docker-compose-abci.yml config -q
docker compose --profile integrated --profile dashboard-integrated -f docker-compose-abci.yml config -q
docker compose --profile integrated -f docker-compose-abci.yml -f docker-compose-abci-bds.yml config -q
XIAN_COMETBFT_RPC_HOST=0.0.0.0 docker compose --profile integrated -f docker-compose-abci.yml config -q
XIAN_COMETBFT_METRICS_HOST=0.0.0.0 XIAN_APP_METRICS_HOST=0.0.0.0 docker compose --profile integrated -f docker-compose-abci.yml config -q
XIAN_POSTGRAPHILE_HOST=0.0.0.0 docker compose --profile integrated -f docker-compose-abci.yml -f docker-compose-abci-bds.yml config -q
docker compose --profile integrated --profile monitoring -f docker-compose-abci.yml -f docker-compose-monitoring.yml config -q
docker compose --profile integrated --profile monitoring -f docker-compose-abci.yml -f docker-compose-abci-bds.yml -f docker-compose-monitoring.yml config -q
docker compose -f docker-compose-abci-dev.yml -f docker-compose-abci-bds.yml config -q
docker compose -f docker-compose-contracting.yml config -q
if [[ -f "${XIAN_INTENTKIT_DIR}/deployment/docker-compose.yml" ]]; then
  intentkit_config_args=(-q)
  if [[ ! -f "${XIAN_INTENTKIT_DIR}/deployment/.env" ]]; then
    intentkit_config_args+=(--no-env-resolution)
  fi
  docker compose \
    --project-directory "${XIAN_INTENTKIT_DIR}/deployment" \
    -f "${XIAN_INTENTKIT_DIR}/deployment/docker-compose.yml" \
    -f docker-compose-intentkit.yml \
    config "${intentkit_config_args[@]}"
fi
uv run --project "${XIAN_CLI_DIR}" python3 "${XIAN_CONFIGS_DIR}/scripts/validate-manifests.py"
./scripts/test-monitoring-rules.sh

printf 'xian-stack validation passed\n'
