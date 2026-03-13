# Xian Stack

`xian-stack` is the Docker and Compose backend for running Xian services locally. It is not the long-term operator UX surface; that belongs in `xian-cli`.

## Workspace Model

This repo now prefers sibling checkouts in the shared `~/xian` workspace:

- `../xian-abci`
- `../xian-contracting`
- `../xian-py`

If those paths do not exist, the Makefile falls back to nested `xian-abci/` and `xian-contracting/` directories when present. Use `make print-env` to inspect the resolved paths.

## Preflight

Run the backend validation before building anything:

```bash
make validate
```

This checks Docker availability, required local repo paths, and Compose rendering for:

- `docker-compose-abci.yml`
- `docker-compose-abci.yml` + `docker-compose-abci-bds.yml`
- `docker-compose-abci.yml` + `docker-compose-abci-dev.yml` + `docker-compose-abci-bds.yml`
- `docker-compose-contracting.yml`

Run the smallest real bring-up and shutdown path with:

```bash
make smoke
```

This builds the base ABCI image, starts the container, initializes and configures CometBFT with a deterministic smoke validator key, starts the node, waits for local RPC and ABCI endpoints, and then shuts the stack back down.

## Common Flows

Base ABCI node:

```bash
make abci-build
make abci-up
make init
make configure CONFIGURE_ARGS='--moniker "<node-name>" --genesis-file-name "genesis-mainnet.json" --validator-privkey "<validator-key>" --seed-node-address "<seed-id@host>" --copy-genesis'
make up
```

ABCI + BDS:

```bash
make abci-bds-build
make abci-bds-up
make init
make configure CONFIGURE_ARGS='--moniker "<node-name>" --genesis-file-name "genesis-mainnet.json" --validator-privkey "<validator-key>" --seed-node-address "<seed-id@host>" --copy-genesis --service-node'
make up-bds
```

Contracting dev shell:

```bash
make contracting-dev-build
make contracting-dev-up
```

## Notes

- The container images are now generic runtime bases. Python repos are installed from mounted workspace paths at container start.
- The stack images now use official Node.js 24 LTS sources. Do not reintroduce the deprecated NodeSource 16 bootstrap path.
- `xian-abci` now depends on `xian-py`, so this repo expects all three Python repos to be available locally.
- `make setup-submodules` still exists for nested checkouts, but the shared workspace layout is the preferred development mode.
- `make smoke` is the runtime contract for this repo. Use it after changing Dockerfiles, compose topology, or backend lifecycle targets.

# Reference

## Docker Networking
- `xian-net`: Main network for service communication and internet access. Exposes ports 26657, 26656, 26660, 5000.
- `xian-db`: Isolated network for database access (PostgreSQL only accessible within this network).

## Docker Compose File Combinations
- `docker-compose-abci.yml`: Base config for Xian node
- `docker-compose-abci-dev.yml`: Adds dev settings
- `docker-compose-abci-bds.yml`: Adds BDS with PostgreSQL

Combine with `-f` flag, e.g.:
```bash
docker-compose -f docker-compose-abci.yml -f docker-compose-abci-bds.yml up
```

## Makefile Shortcuts (Reference)
- `make up` — Start node without BDS
- `make up-bds` — Start node with BDS
- `make abci-up` — Start node (ABCI only)
- `make abci-bds-up` — Start node with BDS
- `make abci-dev-up` — Start dev environment with BDS
- `make down` — Stop node
- `make abci-dev-down` — Stop container

## Advanced: Initializing CometBFT
If you need to initialize CometBFT manually:
```bash
make abci-dev-shell
make init
```

---

For more details, see the comments in each Docker Compose file or the Makefile.
