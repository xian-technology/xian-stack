#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/stack-env.sh"

export_stack_env
prepare_stack_dirs
require_stack_paths
require_docker
require_uv

cd "${stack_root}"

docker compose --profile integrated -f docker-compose-abci.yml config -q
docker compose --profile fidelity -f docker-compose-abci.yml config -q
docker compose --profile integrated --profile dashboard-integrated -f docker-compose-abci.yml config -q
docker compose --profile fidelity --profile dashboard-fidelity -f docker-compose-abci.yml config -q
docker compose --profile integrated -f docker-compose-abci.yml -f docker-compose-abci-bds.yml config -q
docker compose -f docker-compose-abci-dev.yml -f docker-compose-abci-bds.yml config -q
docker compose -f docker-compose-contracting.yml config -q
uv run --project "${XIAN_CLI_DIR}" python3 "${XIAN_CONFIGS_DIR}/scripts/validate-manifests.py"

printf 'xian-stack validation passed\n'
