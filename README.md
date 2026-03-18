# xian-stack

`xian-stack` is the runtime backend for local Xian environments. It owns Docker
images, Compose topology, shell entrypoints, and smoke-tested backend flows. It
does not own the long-term operator UX; that belongs in `xian-cli`.

## Ownership

This repo owns:

- container images under `docker/`
- Compose files for ABCI, BDS, and development paths
- backend lifecycle targets in the `Makefile`
- runtime validation and smoke scripts under `scripts/`

This repo does not own:

- canonical protocol or contract authoring
- end-user network bootstrap UX
- network-specific chain definitions as product-facing configuration

## Workspace Model

The preferred development layout is the shared `~/xian` workspace with sibling
checkouts of:

- `../xian-cli`
- `../xian-abci`
- `../xian-configs`
- `../xian-contracting`
- `../xian-py`

This sibling-workspace model is the supported authoring mode for image builds,
dev shells, and cross-repo smoke coverage. Use `make print-env` to inspect the
resolved paths.

## Validation

Run backend preflight first:

```bash
make validate
```

Run the runtime smoke contract after Dockerfile, Compose, or lifecycle changes:

```bash
make smoke
make smoke-cli
```

`make smoke` is the main safety net for this repo. It builds the base ABCI
image, brings up the minimum stack, initializes CometBFT, configures a
deterministic validator, verifies health, and shuts the stack down again.

`make smoke-cli` is the cross-repo integration gate. It drives the real
operator flow through `xian-cli`: `network join -> node init -> node start ->
node status -> node stop`.

`make validate` now also validates the canonical manifests in `xian-configs`
through `xian-cli`, so the stack checks both Compose topology and the current
cross-repo config contract.

## Preferred Operator Flow

Use `xian-cli` for operator-facing node lifecycle work. From the sibling
workspace, the intended flow is:

```bash
cd ../xian-cli
uv sync --group dev
uv run xian keys validator generate --out-dir ./keys/validator-1
uv run xian network join validator-1 --network mainnet \
  --validator-key-ref ./keys/validator-1/validator_key_info.json \
  --stack-dir ../xian-stack
uv run xian node init validator-1
uv run xian node status validator-1
uv run xian node start validator-1
uv run xian node stop validator-1
```

`xian-stack` is the backend that those commands drive. It should not be the
main user-facing interface for bootstrap or runtime control.

## Backend Flows

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

The backend `node-configure` target now runs the explicit package entrypoint
`python -m xian.cli.configure_node` inside `xian-abci` rather than reaching
into a legacy `src/xian/tools` script path.

The backend `node-status` target returns JSON and is part of the stable backend
contract consumed by `xian-cli`. Keep that output machine-readable; the smoke
test and CLI both depend on it.

`xian-stack` now also exposes a machine-readable backend entrypoint at
`scripts/backend.py`. This is the preferred contract for other tools such as
`xian-cli`:

```bash
python3 ./scripts/backend.py validate
python3 ./scripts/backend.py start --no-service-node
python3 ./scripts/backend.py status --no-service-node
python3 ./scripts/backend.py stop --no-service-node
python3 ./scripts/backend.py smoke
python3 ./scripts/backend.py smoke-cli
python3 ./scripts/backend.py localnet-init --nodes 4 --topology integrated --clean
python3 ./scripts/backend.py localnet-up --wait-for-health --rpc-timeout-seconds 120
python3 ./scripts/backend.py localnet-status
python3 ./scripts/backend.py localnet-workload --scenario dex_mixed --dex-rounds 6
python3 ./scripts/backend.py localnet-memwatch --duration-minutes 10
python3 ./scripts/backend.py localnet-leak-hunt --duration-minutes 10
```

The Makefile remains the local backend implementation and debugging surface,
but the script is the stable control plane boundary.

`make localnet-up` is intentionally fire-and-forget. Use the backend script
with `--wait-for-health` when you need a readiness-aware bring-up in CI or
diagnostic flows.

## Runtime Limits

Container runtime limits now live in the stack contract instead of inside
`xian-contracting`. The default policy is:

- enforce hard memory, swap, PID, and file-descriptor limits at the container
  boundary
- keep those defaults explicit and operator-overridable through environment
  variables
- keep consensus code free of host-specific RSS checks

The main knobs are exported by `make print-env`, including:

- `XIAN_DOCKER_ABCI_MEMORY_LIMIT`, `XIAN_DOCKER_ABCI_MEMORY_RESERVATION`,
  `XIAN_DOCKER_ABCI_MEMORY_SWAP`
- `XIAN_DOCKER_FIDELITY_ABCI_*` and `XIAN_DOCKER_FIDELITY_COMETBFT_*` for the
  split runtime profile
- `XIAN_DOCKER_POSTGRES_*` and `XIAN_DOCKER_POSTGRAPHILE_*`
- `XIAN_LOCALNET_NODE_*` for the integrated multi-node localnet
- `XIAN_LOCALNET_ABCI_*` and `XIAN_LOCALNET_COMETBFT_*` for the split localnet

Example override:

```bash
XIAN_DOCKER_ABCI_MEMORY_LIMIT=3g \
XIAN_DOCKER_ABCI_MEMORY_RESERVATION=2g \
XIAN_DOCKER_ABCI_MEMORY_SWAP=3g \
make smoke
```

For native non-Docker deployments, use host supervision rather than adding
memory heuristics back into `xian-contracting`. See
[`docs/RUNTIME_LIMITS.md`](./docs/RUNTIME_LIMITS.md).

## Runtime Notes

- Runtime node images are immutable. Python packages are installed at build
  time from sibling repos through Docker build contexts; the production/test
  path no longer does startup editable installs or live source mounts.
- `docker-compose-abci-dev.yml` is the only source-mounted development path.
- The default runtime topology is `integrated`: one container, `s6-overlay`,
  and both `xian-abci` and `CometBFT` supervised inside the same node image.
- The optional `fidelity` topology splits `xian-abci` and `CometBFT` into
  separate containers with one process each and `init: true`.
- The optional dashboard is a separate service, not part of the ABCI process.
  It talks to CometBFT RPC over the Compose network and can be started for
  either the integrated or fidelity topology.
- `xian-configs` is copied into the image at `/opt/xian-configs` so canonical
  network bundles and contract presets stay outside `xian-abci`.
- `xian-stack` no longer manages nested repo checkouts or submodules for
  `xian-abci` and `xian-contracting`.
- The node runtime no longer depends on Node.js or PM2. The only Node-based
  service left in this repo is PostGraphile.
- The PostGraphile service runs on the v5 RC line with local `@rc` packages and
  explicit startup scripts instead of removed legacy retry flags.
- In watch mode, PostGraphile also needs a superuser connection so it can
  install watch fixtures.
- Localnet tooling is expected to run through `uv run --project ...` wrappers so
  imports come from installed sibling packages rather than ad hoc `sys.path`
  mutation.
- Localnet supports both `integrated` and `fidelity` topologies through
  `XIAN_LOCALNET_TOPOLOGY`.
- Localnet workload validation is scenario-based. `counter_basic` replaces the
  old burst script as the default alias, and `dex_mixed` is the first
  contract-heavy pack with successful swaps, intentional failures, sampled
  state checks, and `app_hash` verification across nodes.
- Vendored workload contracts live under [`workloads/`](./workloads) so test
  scenarios stay reviewable and do not fetch contract code from GitHub at run
  time.

## Localnet Workloads

Run the default counter sanity scenario:

```bash
make localnet-workload
```

Run the DEX scenario:

```bash
LOCALNET_WORKLOAD_SCENARIO=dex_mixed \
LOCALNET_DEX_ROUNDS=8 \
make localnet-workload
```

Or drive the same flow through the backend contract:

```bash
python3 ./scripts/backend.py localnet-workload \
  --scenario dex_mixed \
  --dex-rounds 8 \
  --state-sample-nodes 2 \
  --app-hash-window 3
```

`make localnet-burst` remains as a thin alias to `counter_basic`, but new
scenario work should go through `localnet-workload`.
