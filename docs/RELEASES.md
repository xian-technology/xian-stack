# Release Pipeline

## Package Releases

The publishable Python packages in the Xian stack now use tag-driven GitHub Actions workflows with PyPI Trusted Publishing.

Recommended release order:

1. `xian-contracting` shared packages:
   - `runtime-types-vX.Y.Z`
   - `native-tracer-vX.Y.Z`
2. `xian-contracting`:
   - `contracting-vX.Y.Z`
3. `xian-py`:
   - `vX.Y.Z`
4. `xian-abci`:
   - `vX.Y.Z`
5. `xian-cli` and `xian-linter`:
   - `vX.Y.Z`
6. `xian-stack`:
   - `vX.Y.Z`

Each release workflow:

- builds wheel and source artifacts
- verifies the built version matches the pushed tag
- publishes to PyPI through Trusted Publishing
- creates a GitHub Release and attaches the built artifacts

Trusted Publishing setup still has to be completed once in PyPI for each project. The workflows expect these GitHub environments:

- `xian-contracting`: `pypi-xian-contracting`
- `xian-runtime-types`: `pypi-xian-runtime-types`
- `xian-native-tracer`: `pypi-xian-native-tracer`
- `xian-abci`: `pypi`
- `xian-cli`: `pypi`
- `xian-py`: `pypi`
- `xian-linter`: `pypi`

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
