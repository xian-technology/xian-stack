# xian-stack

`xian-stack` is the local runtime backend for Xian. It owns Docker images,
Compose topology, monitoring assets, localnet flows, and backend validation. It
is the implementation surface behind local operator workflows, not the primary
operator UX itself.

## Common Workflows

Validate the stack:

```bash
python3 ./scripts/backend.py validate
```

Run a local stack-managed node:

```bash
python3 ./scripts/backend.py start --no-service-node --dashboard --monitoring
python3 ./scripts/backend.py status --no-service-node --dashboard --monitoring
python3 ./scripts/backend.py endpoints --no-service-node --dashboard --monitoring
python3 ./scripts/backend.py health --no-service-node --dashboard --monitoring
python3 ./scripts/backend.py stop --no-service-node --dashboard --monitoring
```

Run a localnet workload:

```bash
python3 ./scripts/backend.py localnet-init --nodes 4 --topology integrated --clean
python3 ./scripts/backend.py localnet-up --wait-for-health --rpc-timeout-seconds 120
python3 ./scripts/backend.py localnet-workload --scenario counter_basic
```

Run the richer 5-validator testnet-shaped governance exercise:

```bash
make localnet-validator-governance
```

Run the broader layered 5-validator e2e harness:

```bash
make localnet-e2e
```

Export or import the standardized BDS recovery snapshot:

```bash
python3 ./scripts/backend.py bds-snapshot-export
python3 ./scripts/backend.py bds-snapshot-import
```

By default the archive lives at:

```bash
.cometbft/snapshots/xian-bds-snapshot.tar.gz
```

Plan or apply a coordinated workspace release:

```bash
python3 ./scripts/release_orchestrator.py plan
python3 ./scripts/release_orchestrator.py apply
```

The `localnet-e2e` harness now also validates direct `currency.approve` /
`transfer_from` behavior and the `permit_authorizer -> approve_from_authorizer`
path against the same 5-validator `testnet`-shaped network.

## Principles

- `xian-stack` owns runtime plumbing, images, and smoke-tested local flows.
  User-facing lifecycle orchestration belongs in `xian-cli`.
- The stable machine-facing contract is `scripts/backend.py`. The `Makefile`
  remains the developer and debugging surface.
- Monitoring, dashboard, and BDS are optional stack layers. They should be easy
  to enable without becoming required to understand the core node.
- `xian-intentkit` can be attached as another optional stack-managed service
  without copying its compose topology into this repo.
- Localnet and smoke flows matter as product safety nets, not just as internal
  convenience scripts.

## Key Directories

- `docker/`: runtime images and container build definitions
- `scripts/`: backend control plane, smoke flows, and localnet tooling
- `monitoring/`: Prometheus, Grafana, dashboards, and alert presets
- `workloads/`: localnet workload fixtures and contracts
- `docs/`: repo-local runtime and release notes

## What It Can Do

- build and run the local Xian runtime in `integrated` or `fidelity` topology
- run canonical-network nodes from pinned published `xian-node` release images
  while keeping source builds available as a local override
- expose a stable machine-facing backend command surface through
  `scripts/backend.py`
- start optional dashboard, BDS, Prometheus, and Grafana layers
- attach a stack-managed `xian-intentkit` deployment while keeping its repo and
  compose files independent
- run smoke checks and CLI-driven smoke flows
- initialize multi-node localnets and drive workload scenarios against them
- ship monitoring dashboards and alert rules that match the validated operator
  profiles

## Backend Surface

The stable backend interface is `scripts/backend.py`. It covers:

- `validate`
- `start`, `stop`, and `status`
- `endpoints` and `health`
- `bds-snapshot-export` and `bds-snapshot-import`
- `smoke` and `smoke-cli`
- `localnet-*` flows

Use the `Makefile` directly for lower-level debugging, image builds, and
developer shell access.

Use `xian-cli` for the human-facing operator workflows built on top of this
backend contract.

## Validation

```bash
make validate
make smoke
make smoke-cli
```

`make smoke` is the main repo safety net. `make smoke-cli` is the cross-repo
operator-flow gate.

## Related Docs

- [AGENTS.md](AGENTS.md)
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- [docs/BACKLOG.md](docs/BACKLOG.md)
- [docs/README.md](docs/README.md)
