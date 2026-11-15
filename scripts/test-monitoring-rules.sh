#!/usr/bin/env bash
# Check the Prometheus configs and unit-test the alert rules with the promtool
# shipped in the stack's own Prometheus image (XIAN_PROMETHEUS_IMAGE).
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/stack-env.sh"

export_stack_env
require_docker

config_dir="${stack_root}/monitoring/prometheus"
cd "${config_dir}"

promtool() {
  docker run --rm --network none --entrypoint promtool \
    -v "${config_dir}:/xian-prometheus:ro" \
    -v "${config_dir}/rules:/etc/prometheus/rules:ro" \
    -w /xian-prometheus \
    "${XIAN_PROMETHEUS_IMAGE}" "$@"
}

promtool check config --syntax-only fidelity.yml integrated.yml
promtool check rules rules/*.yml
promtool test rules tests/*.test.yml

printf 'xian-stack monitoring rules passed\n'
