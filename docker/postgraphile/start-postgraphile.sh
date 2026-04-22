#!/usr/bin/env bash
set -euo pipefail

retry_delay_seconds="${POSTGRAPHILE_RETRY_DELAY_SECONDS:-2}"

args=(
  postgraphile
  -P postgraphile/presets/amber
  -c "${POSTGRAPHILE_CONNECTION}"
  -s "${POSTGRAPHILE_SCHEMA:-public}"
  -n "${POSTGRAPHILE_HOST:-0.0.0.0}"
  -p "${POSTGRAPHILE_PORT:-5000}"
)

while true; do
  if "${args[@]}"; then
    exit 0
  fi

  printf 'postgraphile exited during startup, retrying in %ss\n' "${retry_delay_seconds}" >&2
  sleep "${retry_delay_seconds}"
done
