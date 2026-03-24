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

The backend script accepts optional monitoring state in addition to the
existing service-node and dashboard flags:

```bash
python3 ./scripts/backend.py start --no-service-node --no-dashboard --no-monitoring
python3 ./scripts/backend.py status --no-service-node --no-dashboard --no-monitoring
python3 ./scripts/backend.py stop --no-service-node --no-dashboard --no-monitoring
```

Use the Makefile directly only for backend validation, smoke coverage, or local
debugging:

```bash
make abci-build
make abci-up
make node-init
make node-configure CONFIGURE_ARGS='--moniker "<node-name>" --copy-genesis --genesis-source "<network-name-or-path>" --validator-privkey "<validator-key>"'
make node-status
make node-start
make node-stop
make abci-fidelity-build
make abci-fidelity-up
make node-status-fidelity
make dashboard-build
make dashboard-up
make dashboard-fidelity-build
make dashboard-fidelity-up
```

For BDS-enabled paths:

```bash
make abci-bds-build
make abci-bds-up
make node-start-bds
```

The BDS-enabled stack reads PostgreSQL settings from environment variables
instead of a checked-in config file inside `xian-abci`. The main knobs are:

```bash
export XIAN_BDS_HOST=postgres
export XIAN_BDS_PORT=5432
export XIAN_BDS_DATABASE=xian
export XIAN_BDS_USER=xian
export XIAN_BDS_PASSWORD=xian
```

For the optional explorer/dashboard service:

```bash
make dashboard-build
make dashboard-up
make dashboard-down
make dashboard-fidelity-build
make dashboard-fidelity-up
make dashboard-fidelity-down
```

Developer-only shell targets are intentionally prefixed with `dev-`, for
example `make dev-abci-shell` and `make dev-contracting-shell`.

When documenting operator workflows, prefer `xian-cli` examples over `make
node-*` examples.

## Runtime Notes

- the default runtime topology is `integrated`
- `fidelity` remains available as the split-process topology
- optional monitoring and BDS stacks are part of this repo's runtime surface
- localnet tooling and workloads are expected to run from the sibling workspace
