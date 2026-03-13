# Repository Guidelines

## Scope
- `xian-stack` owns Docker Compose topology, container images, shell entrypoints, and runtime backend operations.
- This repo is a backend, not the long-term operator UX. Public workflows should move into `xian-cli`.
- Keep protocol logic out of this repo unless the runtime backend truly requires it.

## Project Layout
- `Makefile`: backend entrypoints for build, `node-*` runtime operations, and
  `dev-*` shell utilities.
- `docker/`: container image definitions.
- `docker-compose-*.yml`: runtime compositions for ABCI, BDS, and development flows.
- `contracts/`: runtime-local contract mount/data directory.

## Workflow
- The shared `~/xian` sibling workspace is the only supported authoring model.
- The stack expects sibling checkouts of `xian-abci`, `xian-configs`,
  `xian-contracting`, and `xian-py`.
- Keep backend operations stable: prepare, `node-init`, `node-configure`,
  `node-start`, `node-stop`, and container bring-up/down. Do not keep expanding
  the Makefile into the primary operator interface.
- When documenting operator workflows, prefer `xian-cli` commands. Reserve
  direct `make node-*` examples for backend validation, smoke coverage, and
  local debugging.
- Prefer path-driven integration over copying code into images. The containers should consume mounted repos from the shared workspace.
- Keep runtime images on supported LTS toolchains. Do not reintroduce the deprecated NodeSource 16 install path.
- Keep the PostGraphile service on the current v5 RC line with `@rc` package tags until the v5 stable line is available and validated here.
- Keep PostGraphile startup behavior explicit. Prefer health checks and wrapper scripts over legacy retry flags that no longer exist in v5.

## Validation
- Preflight: `make validate`
- Runtime smoke: `make smoke`
- Inspect resolved paths: `make print-env`
- Validate the smallest affected runtime flow after preflight.
- Preferred operator smoke path lives in `xian-cli`; this repo validates the
  backend those commands call.
- Common paths:
  - `make abci-build`
  - `make abci-up`
  - `make node-init`
  - `make node-configure CONFIGURE_ARGS='...'`
  - `make node-start` or `make node-start-bds`
  - `make node-stop`

## Notes
- This repo now has a real smoke harness for the base ABCI path. Keep it green when changing Dockerfiles, compose files, or backend lifecycle targets.
- The stack mounts `xian-configs` into the ABCI container so legacy chain
  fixtures can live outside `xian-abci`.
