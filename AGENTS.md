# Repository Guidelines

## Scope
- `xian-stack` owns Docker Compose topology, container images, shell entrypoints, and runtime backend operations.
- This repo is a backend, not the long-term operator UX. Public workflows should move into `xian-cli`.
- Keep protocol logic out of this repo unless the runtime backend truly requires it.

## Project Layout
- `Makefile`: backend entrypoints for build, init, configure, start, and stop.
- `docker/`: container image definitions.
- `docker-compose-*.yml`: runtime compositions for ABCI, BDS, and development flows.
- `contracts/`: runtime-local contract mount/data directory.

## Workflow
- The shared `~/xian` sibling workspace is the only supported authoring model.
- Keep backend operations stable: prepare, init, start, stop, status. Do not keep expanding the Makefile into the primary operator interface.
- Prefer path-driven integration over copying code into images. The containers should consume mounted repos from the shared workspace.
- Keep runtime images on supported LTS toolchains. Do not reintroduce the deprecated NodeSource 16 install path.
- Keep the PostGraphile service on the current v5 RC line with `@rc` package tags until the v5 stable line is available and validated here.
- Keep PostGraphile startup behavior explicit. Prefer health checks and wrapper scripts over legacy retry flags that no longer exist in v5.

## Validation
- Preflight: `make validate`
- Runtime smoke: `make smoke`
- Inspect resolved paths: `make print-env`
- Validate the smallest affected runtime flow after preflight.
- Common paths:
  - `make abci-build`
  - `make abci-up`
  - `make init`
  - `make configure CONFIGURE_ARGS='...'`
  - `make up` or `make up-bds`
  - `make down`

## Notes
- This repo now has a real smoke harness for the base ABCI path. Keep it green when changing Dockerfiles, compose files, or backend lifecycle targets.
- The stack expects sibling checkouts of `xian-abci`, `xian-contracting`, and `xian-py`.
