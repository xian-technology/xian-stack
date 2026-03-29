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
docker compose --profile fidelity -f docker-compose-abci.yml config -q
docker compose --profile integrated --profile dashboard-integrated -f docker-compose-abci.yml config -q
docker compose --profile fidelity --profile dashboard-fidelity -f docker-compose-abci.yml config -q
docker compose --profile integrated -f docker-compose-abci.yml -f docker-compose-abci-bds.yml config -q
docker compose --profile integrated --profile monitoring -f docker-compose-abci.yml -f docker-compose-monitoring.yml config -q
docker compose --profile integrated --profile monitoring -f docker-compose-abci.yml -f docker-compose-abci-bds.yml -f docker-compose-monitoring.yml config -q
docker compose --profile fidelity --profile monitoring -f docker-compose-abci.yml -f docker-compose-monitoring.yml config -q
docker compose -f docker-compose-abci-dev.yml -f docker-compose-abci-bds.yml config -q
docker compose -f docker-compose-contracting.yml config -q
if [[ -f "${XIAN_INTENTKIT_DIR}/deployment/docker-compose.yml" ]]; then
  cleanup_intentkit_env=0
  if [[ ! -f "${XIAN_INTENTKIT_DIR}/deployment/.env" ]]; then
    : > "${XIAN_INTENTKIT_DIR}/deployment/.env"
    cleanup_intentkit_env=1
  fi
  docker compose \
    --project-directory "${XIAN_INTENTKIT_DIR}/deployment" \
    -f "${XIAN_INTENTKIT_DIR}/deployment/docker-compose.yml" \
    -f docker-compose-intentkit.yml \
    config -q
  if [[ "${cleanup_intentkit_env}" == "1" ]]; then
    rm -f "${XIAN_INTENTKIT_DIR}/deployment/.env"
  fi
fi
uv run --project "${XIAN_CLI_DIR}" python3 "${XIAN_CONFIGS_DIR}/scripts/validate-manifests.py"

printf 'xian-stack validation passed\n'
