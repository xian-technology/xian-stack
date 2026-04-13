# Scripts

## Purpose

This folder contains the stable backend control surface and the local runtime
tooling behind `xian-stack`.

## Key Files

- `backend.py`: the main machine-facing backend command surface
- `stack-env.sh`: shared environment/bootstrap shell helper for Compose flows
- `smoke-stack.sh` and `smoke-cli.sh`: repo-level smoke entrypoints
- `localnet-init.py`: multi-node localnet creation, including local-only
  validator keys, preset-backed genesis selection, native tracer selection, and
  optional BDS wiring
- `localnet-workload.py`: deterministic workload execution against the localnet
- `localnet-e2e.py`: the layered 5-validator testnet-shaped end-to-end program
  that exercises the live stack phase by phase, including direct and
  permit-authorized `currency.approvals` flows, and writes artifacts under
  `.artifacts/localnet-e2e/<run-id>/`
- `localnet_vm_rollout.py`: collects execution-mode, shadow comparison, and
  mismatch counters from a running localnet and emits a rollout report as JSON;
  it reads the Xian app metrics exporter, not the CometBFT metrics endpoint
- `make localnet-vm-e2e`: wrapper around `localnet-e2e.py` that boots the same
  5-validator integrated stack with `xian_vm_v1` in native-authority mode and
  Python shadow comparison enabled; it also enforces the VM rollout mismatch
  budget through the generated `vm_rollout.json` artifact
- `localnet-validator-governance.py`: focused validator/delegation/governance
  exercise against a real 5-validator testnet-shaped localnet, including real
  duplicate-vote evidence injection, governance state-patch activation, and
  announce-leave coverage, with JSON artifacts under
  `.artifacts/localnet-validator-governance/<run-id>/`
- `localnet-burst-test.py`, `localnet-memwatch.py`,
  `localnet-leak-hunt.py`, `localnet-perf-summary.py`: deeper runtime
  investigation helpers
- `release_manifest.py` and `trusted_publishers.py`: release and publisher
  support helpers
- `release_orchestrator.py`: local multi-repo release planner and tag pusher for
  the full Xian workspace

## Notes

- Prefer stable script entrypoints over ad hoc shell fragments in CI or
  operator docs.
- `backend.py` is the contract to build on if another repo or tool wants to
  drive the local stack programmatically.
- `network.json` under `.localnet/` includes local-only validator private keys
  for automated governance flows. Treat it as disposable dev material and do
  not reuse it outside the local test network.
- The validator/governance runner should be executed through `uv` with the
  `xian-abci` project and local `xian-py` package available, preferably through
  `make localnet-validator-governance`.
- `make localnet-vm-report` is the quick operator/debugging path for checking
  whether all localnet nodes agree on VM rollout settings and whether any
  native/shadow mismatches have been observed.
