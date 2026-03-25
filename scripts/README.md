# Scripts

## Purpose

This folder contains the stable backend control surface and the local runtime
tooling behind `xian-stack`.

## Key Files

- `backend.py`: the main machine-facing backend command surface
- `stack-env.sh`: shared environment/bootstrap shell helper for Compose flows
- `smoke-stack.sh` and `smoke-cli.sh`: repo-level smoke entrypoints
- `localnet-init.py` and `localnet-workload.py`: multi-node localnet creation
  and workload execution
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
