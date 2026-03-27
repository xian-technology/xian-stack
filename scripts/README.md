# Scripts

## Purpose

This folder contains the stable backend control surface and the local runtime
tooling behind `xian-stack`.

## Key Files

- `backend.py`: the main machine-facing backend command surface
- `stack-env.sh`: shared environment/bootstrap shell helper for Compose flows
- `smoke-stack.sh` and `smoke-cli.sh`: repo-level smoke entrypoints
- `localnet-init.py`: multi-node localnet creation, including local-only
  validator keys, native tracer selection, and optional BDS wiring
- `localnet-workload.py`: deterministic workload execution against the localnet
- `localnet-e2e.py`: the layered 4-node end-to-end program that exercises the
  live stack phase by phase and writes artifacts under `.localnet/e2e/<run-id>/`
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
