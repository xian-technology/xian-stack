#!/bin/sh
set -eu

retry_delay_seconds="${POSTGRAPHILE_RETRY_DELAY_SECONDS:-2}"
export POSTGRAPHILE_SCHEMA_WAIT_TIMEOUT_SECONDS="${POSTGRAPHILE_SCHEMA_WAIT_TIMEOUT_SECONDS-60}"
export POSTGRAPHILE_REQUIRED_TABLES="${POSTGRAPHILE_REQUIRED_TABLES-addresses,bds_meta,blocks,contracts,events,rewards,shielded_output_tags,shielded_outputs,state,state_changes,state_patches,transactions}"

if [ "${POSTGRAPHILE_SCHEMA_WAIT_TIMEOUT_SECONDS}" != "0" ] && [ -n "${POSTGRAPHILE_REQUIRED_TABLES}" ]; then
  node /usr/src/app/wait-for-bds-schema.mjs
fi

while true; do
  if postgraphile \
    -C "${POSTGRAPHILE_CONFIG:-/usr/src/app/graphile.config.mjs}" \
    -c "${POSTGRAPHILE_CONNECTION}" \
    -s "${POSTGRAPHILE_SCHEMA:-public}" \
    -n "${POSTGRAPHILE_HOST:-0.0.0.0}" \
    -p "${POSTGRAPHILE_PORT:-5000}"; then
    exit 0
  fi

  printf 'postgraphile exited during startup, retrying in %ss\n' "${retry_delay_seconds}" >&2
  sleep "${retry_delay_seconds}"
done
