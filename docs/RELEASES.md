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
| `xian-contracting` | `xian-technology` | `xian-contracting` | `release.yml` | `pypi-xian-contracting` |
| `xian-runtime-types` | `xian-technology` | `xian-contracting` | `release.yml` | `pypi-xian-runtime-types` |
| `xian-native-tracer` | `xian-technology` | `xian-contracting` | `release.yml` | `pypi-xian-native-tracer` |
| `xian-abci` | `xian-technology` | `xian-abci` | `release.yml` | `pypi` |
| `xian-cli` | `xian-technology` | `xian-cli` | `release.yml` | `pypi` |
| `xian-py` | `xian-technology` | `xian-py` | `release.yml` | `pypi` |
| `xian-linter` | `xian-technology` | `xian-linter` | `release.yml` | `pypi` |

Notes:

- the workflow value entered in PyPI is the workflow filename, `release.yml`
- the environment value is optional on the PyPI side, but Xian uses it intentionally and it should match exactly
- if a package name does not exist on PyPI yet, create a pending publisher for it first; that reserves nothing until the first successful publish
- if a repo or workflow file is renamed later, update the PyPI publisher entry to match or releases will fail

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
