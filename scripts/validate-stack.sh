#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/stack-env.sh"

export_stack_env
prepare_stack_dirs
require_stack_paths
require_docker

cd "${stack_root}"

docker compose -f docker-compose-abci.yml config -q
docker compose -f docker-compose-abci.yml -f docker-compose-abci-bds.yml config -q
docker compose -f docker-compose-abci.yml -f docker-compose-abci-dev.yml -f docker-compose-abci-bds.yml config -q
docker compose -f docker-compose-contracting.yml config -q

printf 'xian-stack validation passed\n'
