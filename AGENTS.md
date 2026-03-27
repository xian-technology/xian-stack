# Repository Guidelines

## Scope
- `xian-stack` owns Docker Compose topology, container images, shell entrypoints, and runtime backend operations.
- This repo is a backend, not the long-term operator UX. Public workflows should move into `xian-cli`.
- Keep protocol logic out of this repo unless the runtime backend truly requires it.

## Shared Convention
- Follow the shared repo convention in `xian-meta/docs/REPO_CONVENTIONS.md`.
- Keep this repo aligned with that standard for root docs, backlog notes, and major folder entrypoints.
- Follow the shared change workflow in `xian-meta/docs/CHANGE_WORKFLOW.md`.
- Before push, review downstream impact on `xian-deploy` and `xian-docs-web`, and run the local validation path from this file.

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
  `node-start`, `node-stop`, `node-status`, and container bring-up/down. Do not
  keep expanding the Makefile into the primary operator interface.
- Prefer package entrypoints such as `python -m xian.cli.configure_node` over
  `cd`-ing into legacy script locations inside `xian-abci`.
- When documenting operator workflows, prefer `xian-cli` commands. Reserve
  direct `make node-*` examples for backend validation, smoke coverage, and
  local debugging.
- Prefer path-driven integration over copying code into images. The containers should consume mounted repos from the shared workspace.
- Keep runtime images on supported LTS toolchains. Do not reintroduce the deprecated NodeSource 16 install path.
- Keep the PostGraphile service on the stable v5 line once validated here.
- The current plugin ecosystem is mixed: `postgraphile` should track stable
  v5, while plugins without a stable v5 release may remain on their current RC
  line until upstream publishes stable versions.
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
  - `make node-status`
  - `make node-start` or `make node-start-bds`
  - `make node-stop`

## Notes
- This repo now has a real smoke harness for the base ABCI path. Keep it green when changing Dockerfiles, compose files, or backend lifecycle targets.
- `make node-status` is part of the backend contract consumed by `xian-cli`.
  Keep it emitting machine-readable JSON.
- The stack mounts `xian-configs` into the ABCI container so legacy chain
  fixtures can live outside `xian-abci`.
