# Repository Guidelines

## Scope
- `xian-stack` owns Docker Compose topology, container images, shell entrypoints, and runtime backend operations.
- This repo is a backend, not the long-term operator UX. Public workflows should move into `xian-cli`.
- Keep protocol logic out of this repo unless the runtime backend truly requires it.

## Project Layout
- `Makefile`: current backend entrypoints for setup, build, init, configure, start, and stop.
- `docker/`: container image definitions.
- `docker-compose-*.yml`: runtime compositions for ABCI, BDS, and development flows.
- `contracts/`: stack-local contract assets.
- `xian-abci/` and `xian-contracting/`: vendored runtime checkouts used by this repo, not the preferred authoring copies in the shared workspace.

## Workflow
- When editing protocol or contract code, prefer the sibling repos in `~/xian`, not the nested checkouts here.
- Only change submodule pins or nested checkout state intentionally.
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
- The nested `xian-abci` and `xian-contracting` directories are runtime inputs. Keep the shared workspace repos as the primary development sources.
