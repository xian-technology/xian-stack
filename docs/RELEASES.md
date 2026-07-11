# Release Pipeline

## Coordinated Releases

The releasable repos in the Xian workspace now use tag-driven GitHub Actions workflows.
Python packages publish through PyPI Trusted Publishing, npm packages publish from
tag pushes, and `xian-stack` publishes GHCR images from an explicit manifest.

Recommended release order:

1. `xian-contracting` shared packages:
   - `compiler-core-vX.Y.Z`
   - `accounts-vX.Y.Z`
   - `runtime-types-vX.Y.Z`
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
   - publishes `@xian-tech/types`, `@xian-tech/client`,
     `@xian-tech/provider`, and `@xian-tech/web-kit` from the same tag
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

Use the default stable flow unless a prerelease was explicitly requested. Do not
pass `--beta` for normal releases.

For the beta channel, pass the channel option before the subcommand:

```bash
python3 ./scripts/release_orchestrator.py --beta plan
python3 ./scripts/release_orchestrator.py --beta apply
```

The orchestrator:

- fetches `origin` and tags for every relevant repo
- detects changed release units against `origin/main`
- reuses a pre-bumped version if `main` already carries it
- otherwise applies a conservative patch bump
- checks the latest GitHub check runs for every `origin/main` ref it will use
  before any release commit or tag is pushed
- creates release-prep commits only where the source tree still needs version edits
- pushes tags in dependency order
- updates `release-manifest.json` and tags `xian-stack` last
- ignores docs-only `xian-stack` changes for image-release planning

`apply` refuses to run if any repo it needs is dirty, off `main`, or ahead/behind
`origin/main`. It also refuses to run when the latest GitHub check run for any
required release input ref is missing, pending, cancelled, or failed. That keeps
the release set anchored to the exact state already on GitHub and prevents
tagging code that has not passed GitHub validation.

The `xian-js` release unit is intentionally repo-wide. Changes under
`xian-js/packages/web-kit/` are released by the `xian-js` tag workflow, which
verifies all JS package versions match the tag and publishes all four npm
packages together. If `web-kit` becomes a standalone repository later, add it as
a separate `ReleaseUnit` in `scripts/release_orchestrator.py`.
Release-process and root documentation changes in `xian-js`, such as
`.github/`, `README.md`, and `docs/`, do not create SDK package releases.
Package-facing changes under `packages/` remain release-relevant.

All non-stack workflows create the GitHub release from the pushed tag automatically.
No separate “publish a GitHub release first” step is needed.

## Independent App Releases

`xian-wallet-mobile` and `xian-contracting-hub-web` also have tag-driven GitHub
Release workflows, but they are not part of the coordinated stack image release
order. Release them only when their own repo has release-relevant changes, after
their `main` validation is green:

- `xian-wallet-mobile`: push `vX.Y.Z`; the workflow validates version metadata,
  builds sibling `xian-js`, runs typecheck/tests, exports the Android bundle,
  and attaches checksummed artifacts to the GitHub Release.
- `xian-contracting-hub-web`: push `vX.Y.Z`; the workflow validates
  `pyproject.toml`, runs Ruff/unit/Reflex export checks, builds Python
  distributions, and attaches them to the GitHub Release.

## Python Package Releases

Each Python release workflow:

- builds wheel and source artifacts
- verifies the built version matches the pushed tag
- publishes to PyPI through Trusted Publishing
- creates a GitHub Release and attaches the built artifacts

Trusted Publishing setup still has to be completed once in PyPI for each project. The workflows expect these GitHub environments:

- `xian-tech-contracting`: `pypi-xian-contracting`
- `xian-tech-accounts`: `pypi-xian-accounts`
- `xian-tech-runtime-types`: `pypi-xian-runtime-types`
- `xian-tech-compiler-core`: `pypi-xian-compiler-core`
- `xian-tech-zk`: `pypi-xian-zk`
- `xian-tech-abci`: `pypi`
- `xian-tech-cli`: `pypi`
- `xian-tech-py`: `pypi`
- `xian-tech-linter`: `pypi`
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
| `xian-tech-accounts` | `xian-technology` | `xian-contracting` | `release.yml` | `pypi-xian-accounts` |
| `xian-tech-contracting` | `xian-technology` | `xian-contracting` | `release.yml` | `pypi-xian-contracting` |
| `xian-tech-runtime-types` | `xian-technology` | `xian-contracting` | `release.yml` | `pypi-xian-runtime-types` |
| `xian-tech-compiler-core` | `xian-technology` | `xian-contracting` | `release.yml` | `pypi-xian-compiler-core` |
| `xian-tech-zk` | `xian-technology` | `xian-contracting` | `release.yml` | `pypi-xian-zk` |
| `xian-tech-abci` | `xian-technology` | `xian-abci` | `release.yml` | `pypi` |
| `xian-tech-cli` | `xian-technology` | `xian-cli` | `release.yml` | `pypi` |
| `xian-tech-py` | `xian-technology` | `xian-py` | `release.yml` | `pypi` |
| `xian-tech-linter` | `xian-technology` | `xian-linter` | `release.yml` | `pypi` |
| `xian-tech-intentkit` | `xian-technology` | `xian-intentkit` | `release.yml` | `pypi` |

Notes:

- the workflow value entered in PyPI is the workflow filename, `release.yml`
- the environment value is optional on the PyPI side, but Xian uses it intentionally and it should match exactly
- if a package name does not exist on PyPI yet, create a pending publisher for it first; that reserves nothing until the first successful publish
- if a repo or workflow file is renamed later, update the PyPI publisher entry to match or releases will fail

## npm Package Releases

`xian-js` and `xian-wallet-browser` follow the same tag-push model, but publish
to npm instead of PyPI. A `xian-js` tag publishes these npm packages together:

- `@xian-tech/types`
- `@xian-tech/client`
- `@xian-tech/provider`
- `@xian-tech/web-kit`

Their workflows:

- build and validate the workspace from the tagged commit
- verify the package versions match the pushed tag
- publish the packed artifacts to npm
- create a GitHub Release with the built artifacts attached

## Stack Image Releases

`xian-stack` publishes release images to GHCR from a committed release manifest in `release-manifest.json`.

The manifest pins:

- exact Git refs for `xian-abci`, `xian-configs`, `xian-contracting`, and `xian-py`
- digest-pinned Python, Go, Rust, and uv images
- a Debian snapshot timestamp for all release-image `apt-get update` calls
- a fixed `SOURCE_DATE_EPOCH` for deterministic wheel archives and image layers
- pinned Python build-tool versions for `pip`, `packaging`, `wheel`, and `maturin`
- the CometBFT version, source archive URL, and source SHA256
- the s6-overlay version plus architecture-specific archive SHA256 values
- the output image names

The Python runtime dependency set for the node image is exported from
`xian-abci/uv.lock` into `docker/python-runtime-requirements.txt`. Regenerate
that file with:

```bash
python3 ./scripts/export_python_runtime_requirements.py
```

The Docker build uses that file with `--require-hashes` and installs external
runtime wheels offline from the staged wheelhouse. Local Xian wheels are then
installed with `--no-deps` so the final image does not depend on live resolver
decisions from PyPI.

The image workflow:

1. validates the manifest
2. runs the cross-repo release safety gate:
   - `xian-contracting/scripts/validate-release.sh`
   - `xian-abci/scripts/validate-release.sh`
   - `xian-stack/scripts/validate-stack.sh`
   - `make localnet-parallel-e2e`
   - `make localnet-node-report`
   - `make localnet-protocol-safety`
3. checks out the exact pinned component refs
4. builds and publishes:
   - `ghcr.io/<owner>/xian-node`
   - `ghcr.io/<owner>/xian-node-split`
5. signs the published images by digest through Sigstore keyless signing
6. attaches signed release assets to the GitHub Release:
   - `release-manifest.json`
   - `image-release.txt`
   - `image-release.json`
   - per-asset `.sigstore.json` bundle files
7. runs a post-publish reproducibility audit that rebuilds the release images
   per platform and compares the observed `linux/amd64` and `linux/arm64`
   digests with the digests recorded in `image-release.json`

Update the manifest before tagging `xian-stack` so the published image is reproducible from explicit component refs.

### What `image-release.json` Adds

`image-release.json` is the machine-facing release summary. It records:

- the published integrated and split image repositories
- the top-level multi-arch digest for each image
- the per-platform image manifest digests used by the reproducibility verifier

That file is intentionally easier for automation to consume than the plain-text
`image-release.txt` summary.

### Verifying A Published Image

Supported operator paths:

- Linux: full verification is supported with Docker Engine or Docker Desktop,
  Buildx, Cosign, Python 3, and the sibling Xian repos checked out under one
  workspace root. This path can verify Sigstore signatures and run the local
  reproducibility rebuild for `linux/amd64` and `linux/arm64`.
- macOS: signature and release-asset verification are supported with Cosign.
  The reproducibility rebuild is supported when Docker Desktop Buildx can build
  both `linux/amd64` and `linux/arm64` images through the configured builder.
  Expect the rebuild to take longer on non-native platforms.

Download the release assets for the tag first:

- `release-manifest.json`
- `image-release.json`
- `release-manifest.json.sigstore.json`
- `image-release.json.sigstore.json`

Use Cosign against the immutable digest, not a floating tag:

```bash
cosign verify \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity-regexp 'https://github.com/xian-technology/xian-stack/.github/workflows/release.yml@refs/tags/.*' \
  ghcr.io/xian-technology/xian-node@sha256:<digest>

cosign verify \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity-regexp 'https://github.com/xian-technology/xian-stack/.github/workflows/release.yml@refs/tags/.*' \
  ghcr.io/xian-technology/xian-node-split@sha256:<digest>
```

Use the signed release assets to verify the recorded manifest and digest set:

```bash
cosign verify-blob \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity-regexp 'https://github.com/xian-technology/xian-stack/.github/workflows/release.yml@refs/tags/.*' \
  --bundle release-manifest.json.sigstore.json \
  release-manifest.json

cosign verify-blob \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity-regexp 'https://github.com/xian-technology/xian-stack/.github/workflows/release.yml@refs/tags/.*' \
  --bundle image-release.json.sigstore.json \
  image-release.json
```

The release verifier is also available as a standalone helper:

```bash
python3 ./scripts/verify_release_reproducibility.py \
  --manifest ./release-manifest.json \
  --image-release ./image-release.json \
  --workspace-root /path/to/xian
```

The reproducibility audit is a hard GitHub release gate. Images are built and
published before the audit because the verifier compares against the published
platform digests, but the GitHub release is blocked unless the rebuild passes.
Keep `release-manifest.json` current before tagging: base images, the uv helper
image, the Debian snapshot timestamp, Python packaging tool versions, CometBFT
source metadata, and s6-overlay archive checksums are release inputs.

Canonical network manifests in `xian-configs/networks/*/manifest.json` can then
pin those published images by digest, and `xian-cli network join` will carry
those pinned image references into generated node profiles by default. Operators
can still override that posture and fall back to local `xian-stack` builds for
development or custom testing.

Use normal tags such as `v0.1.0` for stable image releases and prerelease tags
such as `v0.1.0-alpha.1` while the node image line is still maturing. Tags that
contain a hyphen are published as GitHub prereleases automatically.
