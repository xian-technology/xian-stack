#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/stack-env.sh"

export_stack_env
prepare_stack_dirs
require_stack_paths
require_docker
require_uv

repo_validation=1
validator_governance=1
vm_report=1
keep_localnet=0

while (($#)); do
  case "$1" in
    --skip-repo-validation)
      repo_validation=0
      ;;
    --skip-validator-governance)
      validator_governance=0
      ;;
    --skip-vm-report)
      vm_report=0
      ;;
    --keep-localnet)
      keep_localnet=1
      ;;
    *)
      printf 'unknown argument: %s\n' "$1" >&2
      exit 1
      ;;
  esac
  shift
done

restore_ci_workspace_ownership() {
  if [[ "${GITHUB_ACTIONS:-}" != "true" ]]; then
    return 0
  fi

  local paths=()
  local path
  for path in .localnet .artifacts docker-compose-localnet.yml; do
    if [[ -e "${path}" ]]; then
      paths+=("${path}")
    fi
  done

  if ((${#paths[@]})); then
    sudo chown -R "$(id -u):$(id -g)" "${paths[@]}" || true
  fi
}

cleanup() {
  if [[ "${keep_localnet}" != "1" ]]; then
    (cd "${stack_root}" && make localnet-down >/dev/null 2>&1) || true
  fi
  (cd "${stack_root}" && restore_ci_workspace_ownership) || true
}
trap cleanup EXIT

run_step() {
  printf '\n==> %s\n' "$1"
  shift
  "$@"
}

cd "${stack_root}"

if [[ "${repo_validation}" == "1" ]]; then
  run_step "Validate xian-contracting release gate" \
    bash -lc "cd '${XIAN_CONTRACTING_DIR}' && ./scripts/validate-release.sh"
  run_step "Validate xian-abci release gate" \
    bash -lc "cd '${XIAN_ABCI_DIR}' && ./scripts/validate-release.sh"
  run_step "Validate xian-stack repo" ./scripts/validate-stack.sh
fi

run_step "Run localnet VM-native e2e" make localnet-vm-e2e

if [[ "${vm_report}" == "1" ]]; then
  run_step "Collect localnet VM rollout report" make localnet-vm-report
fi

if [[ "${validator_governance}" == "1" ]]; then
  run_step "Reset localnet before validator governance" make localnet-down
  restore_ci_workspace_ownership
  run_step "Run validator governance localnet" make localnet-validator-governance
fi
