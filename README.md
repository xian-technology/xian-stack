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

- `../xian-abci`
- `../xian-configs`
- `../xian-contracting`
- `../xian-py`

This sibling-workspace model is the only supported authoring mode. Use
`make print-env` to inspect the resolved paths.

## Validation

Run backend preflight first:

```bash
make validate
```

Run the runtime smoke contract after Dockerfile, Compose, or lifecycle changes:

```bash
make smoke
```

`make smoke` is the main safety net for this repo. It builds the base ABCI
image, brings up the minimum stack, initializes CometBFT, configures a
deterministic validator, verifies health, and shuts the stack down again.

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
make node-configure CONFIGURE_ARGS='--moniker "<node-name>" --copy-genesis --genesis-file-name "<genesis.json>" --validator-privkey "<validator-key>"'
make node-start
make node-stop
```

For BDS-enabled paths:

```bash
make abci-bds-build
make abci-bds-up
make node-start-bds
```

Developer-only shell targets are intentionally prefixed with `dev-`, for
example `make dev-abci-shell` and `make dev-contracting-shell`.

When documenting operator workflows, prefer `xian-cli` examples over `make
node-*` examples.

The backend `node-configure` target now runs the explicit package entrypoint
`python -m xian.cli.configure_node` inside `xian-abci` rather than reaching
into a legacy `src/xian/tools` script path.

## Runtime Notes

- Runtime images consume mounted sibling repos from the shared workspace.
- The stack mounts `xian-configs` into the ABCI container so legacy exported
  genesis fixtures and contract manifests stay outside `xian-abci`.
- `xian-stack` no longer manages nested repo checkouts or submodules for
  `xian-abci` and `xian-contracting`.
- The stack images use official Node.js 24 LTS sources.
- The PostGraphile service runs on the v5 RC line with local `@rc` packages and
  explicit startup scripts instead of removed legacy retry flags.
- In watch mode, PostGraphile also needs a superuser connection so it can
  install watch fixtures.
