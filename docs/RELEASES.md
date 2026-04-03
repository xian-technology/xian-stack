# Release Pipeline

## Coordinated Releases

The releasable repos in the Xian workspace now use tag-driven GitHub Actions workflows.
Python packages publish through PyPI Trusted Publishing, npm packages publish from
tag pushes, and `xian-stack` publishes GHCR images from an explicit manifest.

Recommended release order:

1. `xian-contracting` shared packages:
   - `accounts-vX.Y.Z`
   - `runtime-types-vX.Y.Z`
   - `native-tracer-vX.Y.Z`
   - `zk-vX.Y.Z`
2. `xian-contracting`:
   - `contracting-vX.Y.Z`
3. `xian-py`:
   - `vX.Y.Z`
4. `xian-abci`:
   - `vX.Y.Z`
5. `xian-cli`:
   - `vX.Y.Z`
6. `xian-linter`:
   - `vX.Y.Z`
7. `xian-js`:
   - `vX.Y.Z`
8. `xian-wallet-browser`:
   - `vX.Y.Z`
9. `xian-intentkit`:
   - `vX.Y.Z`
10. `xian-stack`:
   - `vX.Y.Z`

`xian-wallet-browser` intentionally rolls after `xian-js` because its release build
checks out sibling `xian-js` sources. `xian-stack` stays last because its release
manifest pins exact component refs and should be updated after upstream release
commits exist on `main`.

## Release Orchestrator

Use the local orchestrator in `xian-stack` when you want one command to release
every repo whose `origin/main` changed since its latest relevant tag:

```bash
cd /Users/endogen/Projekte/xian/xian-stack
python3 ./scripts/release_orchestrator.py plan
python3 ./scripts/release_orchestrator.py apply
```

The orchestrator:

- fetches `origin` and tags for every relevant repo
- detects changed release units against `origin/main`
- reuses a pre-bumped version if `main` already carries it
- otherwise applies a conservative patch bump
- creates release-prep commits only where the source tree still needs version edits
- pushes tags in dependency order
- updates `release-manifest.json` and tags `xian-stack` last

`apply` refuses to run if any repo it needs is dirty, off `main`, or ahead/behind
`origin/main`. That keeps the release set anchored to the exact state already on GitHub.

All non-stack workflows create the GitHub release from the pushed tag automatically.
No separate “publish a GitHub release first” step is needed.

## Python Package Releases

Each Python release workflow:

- builds wheel and source artifacts
- verifies the built version matches the pushed tag
- publishes to PyPI through Trusted Publishing
- creates a GitHub Release and attaches the built artifacts

Trusted Publishing setup still has to be completed once in PyPI for each project. The workflows expect these GitHub environments:

- `xian-contracting`: `pypi-xian-contracting`
- `xian-accounts`: `pypi-xian-accounts`
- `xian-runtime-types`: `pypi-xian-runtime-types`
- `xian-native-tracer`: `pypi-xian-native-tracer`
- `xian-zk`: `pypi-xian-zk`
- `xian-abci`: `pypi`
- `xian-cli`: `pypi`
- `xian-py`: `pypi`
- `xian-linter`: `pypi`
- `xian-intentkit`: `pypi`

### PyPI Trusted Publisher Setup

The PyPI-side registration is still a manual step. Use the PyPI project settings page for each package:

- existing project: `https://pypi.org/manage/project/<project-name>/settings/publishing/`
- new project: use the pending publisher flow in PyPI before the first release

The helper below prints the exact values to enter:

```bash
python3 ./scripts/trusted_publishers.py markdown
```

Current publisher matrix:

| PyPI project | GitHub owner | GitHub repo | Workflow filename | Environment |
| --- | --- | --- | --- | --- |
| `xian-accounts` | `xian-technology` | `xian-contracting` | `release.yml` | `pypi-xian-accounts` |
| `xian-contracting` | `xian-technology` | `xian-contracting` | `release.yml` | `pypi-xian-contracting` |
| `xian-runtime-types` | `xian-technology` | `xian-contracting` | `release.yml` | `pypi-xian-runtime-types` |
| `xian-native-tracer` | `xian-technology` | `xian-contracting` | `release.yml` | `pypi-xian-native-tracer` |
| `xian-tech-zk` | `xian-technology` | `xian-contracting` | `release.yml` | `pypi-xian-zk` |
| `xian-abci` | `xian-technology` | `xian-abci` | `release.yml` | `pypi` |
| `xian-cli` | `xian-technology` | `xian-cli` | `release.yml` | `pypi` |
| `xian-py` | `xian-technology` | `xian-py` | `release.yml` | `pypi` |
| `xian-linter` | `xian-technology` | `xian-linter` | `release.yml` | `pypi` |
| `xian-tech-intentkit` | `xian-technology` | `xian-intentkit` | `release.yml` | `pypi` |

Notes:

- the workflow value entered in PyPI is the workflow filename, `release.yml`
- the environment value is optional on the PyPI side, but Xian uses it intentionally and it should match exactly
- if a package name does not exist on PyPI yet, create a pending publisher for it first; that reserves nothing until the first successful publish
- if a repo or workflow file is renamed later, update the PyPI publisher entry to match or releases will fail

## npm Package Releases

`xian-js` and `xian-wallet-browser` follow the same tag-push model, but publish to npm
instead of PyPI.

Their workflows:

- build and validate the workspace from the tagged commit
- verify the package versions match the pushed tag
- publish the packed artifacts to npm
- create a GitHub Release with the built artifacts attached

## Stack Image Releases

`xian-stack` publishes release images to GHCR from a committed release manifest in `release-manifest.json`.

The manifest pins:

- exact Git refs for `xian-abci`, `xian-configs`, `xian-contracting`, and `xian-py`
- the Python base image
- the CometBFT version
- the s6-overlay version
- the output image names

The image workflow:

1. validates the manifest
2. checks out the exact pinned component refs
3. builds and publishes:
   - `ghcr.io/<owner>/xian-node`
   - `ghcr.io/<owner>/xian-node-split`
4. attaches the manifest and resolved image digests to the GitHub Release

Update the manifest before tagging `xian-stack` so the published image is reproducible from explicit component refs.

Canonical network manifests in `xian-configs/networks/*/manifest.json` can then
pin those published images by digest, and `xian-cli network join` will carry
those pinned image references into generated node profiles by default. Operators
can still override that posture and fall back to local `xian-stack` builds for
development or custom testing.

Use normal tags such as `v0.1.0` for stable image releases and prerelease tags
such as `v0.1.0-alpha.1` while the node image line is still maturing. Tags that
contain a hyphen are published as GitHub prereleases automatically.
