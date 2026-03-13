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
- `../xian-contracting`
- `../xian-py`

If those paths do not exist, the Makefile can still fall back to nested
checkouts when present. Use `make print-env` to inspect the resolved paths.

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

## Backend Flows

Representative backend operations:

```bash
make abci-build
make abci-up
make init
make configure CONFIGURE_ARGS='--moniker "<node-name>" --copy-genesis --genesis-file-name "<genesis.json>" --validator-privkey "<validator-key>"'
make up
make down
```

For BDS-enabled paths:

```bash
make abci-bds-build
make abci-bds-up
make up-bds
```

`xian-cli` should increasingly drive these operations instead of users calling
them manually.

## Runtime Notes

- Runtime images consume mounted sibling repos from the shared workspace.
- The stack images use official Node.js 24 LTS sources.
- The PostGraphile service runs on the v5 RC line with local `@rc` packages and
  explicit startup scripts instead of removed legacy retry flags.
- In watch mode, PostGraphile also needs a superuser connection so it can
  install watch fixtures.
