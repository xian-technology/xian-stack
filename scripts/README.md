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
