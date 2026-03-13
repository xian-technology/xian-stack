#!/usr/bin/env bash

stack_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

resolve_repo_dir() {
  local name="$1"
  local explicit_value="${2:-}"
  local sibling_path="${stack_root}/../${name}"
  local nested_path="${stack_root}/${name}"

  if [[ -n "${explicit_value}" ]]; then
    printf '%s\n' "${explicit_value}"
    return
  fi

  if [[ -d "${sibling_path}" ]]; then
    printf '%s\n' "${sibling_path}"
    return
  fi

  printf '%s\n' "${nested_path}"
}

export_stack_env() {
  export XIAN_ABCI_DIR="${XIAN_ABCI_DIR:-$(resolve_repo_dir xian-abci "${XIAN_ABCI_DIR:-}")}"
  export XIAN_CONTRACTING_DIR="${XIAN_CONTRACTING_DIR:-$(resolve_repo_dir xian-contracting "${XIAN_CONTRACTING_DIR:-}")}"
  export XIAN_PY_DIR="${XIAN_PY_DIR:-$(resolve_repo_dir xian-py "${XIAN_PY_DIR:-}")}"
  export XIAN_COMETBFT_HOME="${XIAN_COMETBFT_HOME:-${stack_root}/.cometbft}"
  export XIAN_BDS_DATA_DIR="${XIAN_BDS_DATA_DIR:-${stack_root}/.bds.db}"
  export XIAN_CONTRACTS_DIR="${XIAN_CONTRACTS_DIR:-${stack_root}/contracts}"
}

require_stack_paths() {
  local required_paths=(
    "${XIAN_ABCI_DIR}"
    "${XIAN_CONTRACTING_DIR}"
    "${XIAN_PY_DIR}"
    "${XIAN_CONTRACTS_DIR}"
  )

  for path in "${required_paths[@]}"; do
    if [[ ! -d "${path}" ]]; then
      printf 'missing required directory: %s\n' "${path}" >&2
      return 1
    fi
  done
}

require_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    printf 'docker is required but not installed\n' >&2
    return 1
  fi

  docker compose version >/dev/null
}

prepare_stack_dirs() {
  mkdir -p "${XIAN_COMETBFT_HOME}" "${XIAN_BDS_DATA_DIR}" "${XIAN_CONTRACTS_DIR}"
}
