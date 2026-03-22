# Architecture

`xian-stack` owns the runtime backend shape for Xian.

Main areas:

- `docker/`: image definitions
- `monitoring/`: Prometheus and Grafana assets
- `scripts/`: backend entrypoints and validation helpers
- `workloads/`: localnet and workload scenarios
- `contracts/`: runtime-local contract fixtures or mounts

Dependency direction:

- consumes `xian-abci`, `xian-contracting`, `xian-configs`, and `xian-py`
- is consumed by `xian-cli` and `xian-deploy`

