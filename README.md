# xian-stack

`xian-stack` is the runtime backend for local Xian environments. It owns Docker
images, Compose topology, backend control scripts, and smoke-tested runtime
flows. It does not own the long-term operator UX; that belongs in `xian-cli`.

## Scope

This repo owns:

- container images and Compose topology
- backend lifecycle targets and helper scripts
- localnet, smoke, and backend validation flows
- optional monitoring and BDS runtime wiring

This repo does not own:

- canonical protocol or contract authoring rules
- end-user network bootstrap UX
- network-specific chain definitions as product-facing configuration

## Key Directories

- `docker/`: runtime images and container build definitions
- `scripts/`: backend control plane, smoke flows, and localnet tooling
- `monitoring/`: Prometheus and Grafana configuration
- `workloads/`: localnet workload fixtures and contracts
- `docs/`: repo-local runtime and release notes

## Validation

```bash
make validate
make smoke
make smoke-cli
```

`make smoke` is the main safety net for this repo. `make smoke-cli` is the
cross-repo operator-flow gate.

## Related Docs

- [AGENTS.md](AGENTS.md)
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- [docs/BACKLOG.md](docs/BACKLOG.md)
- [docs/README.md](docs/README.md)

## Workspace Model

The preferred development layout is a shared `~/xian` workspace with sibling
checkouts of `xian-cli`, `xian-abci`, `xian-configs`, `xian-contracting`, and
`xian-py`.

## Preferred Operator Flow

Use `xian-cli` for operator-facing node lifecycle work. Use `xian-stack`
directly for backend validation, smoke coverage, local debugging, and localnet
workloads.

## Backend Flows

The stable backend control surface is `scripts/backend.py`:

```bash
python3 ./scripts/backend.py validate
python3 ./scripts/backend.py smoke
python3 ./scripts/backend.py smoke-cli
python3 ./scripts/backend.py localnet-init --nodes 4 --topology integrated --clean
python3 ./scripts/backend.py localnet-up --wait-for-health --rpc-timeout-seconds 120
python3 ./scripts/backend.py localnet-workload --scenario counter_basic
```

The Makefile remains the local implementation and debugging surface, but other
tools should prefer the backend script contract.

## Runtime Notes

- the default runtime topology is `integrated`
- `fidelity` remains available as the split-process topology
- optional monitoring and BDS stacks are part of this repo's runtime surface
- localnet tooling and workloads are expected to run from the sibling workspace
