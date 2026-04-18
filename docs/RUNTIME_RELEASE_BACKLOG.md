# Runtime And Release Backlog

These items were intentionally deferred after the runtime topology cleanup.
They are the next release-engineering hardening phase, not part of the
integrated-vs-fidelity runtime refactor itself.

## Dependency And Artifact Pinning

- Done: OCI base images are now digest-pinned in Dockerfiles and release manifests.
- Done: the Rust build toolchain now comes from a digest-pinned Rust base image.
- Done: CometBFT release builds now use a checksum-pinned source archive.
- Done: s6-overlay archives are now verified against manifest-pinned SHA256 values.
- Done: node image Python runtime installs now come from a hash-pinned requirements export derived from `xian-abci/uv.lock`.
- Done: Python wheel archives now use a manifest-pinned `SOURCE_DATE_EPOCH` plus pinned `pip` / `wheel` / `maturin` tool versions.
- Audit any remaining floating runtime dependencies and either pin them or
  document why they intentionally float.

## SBOM And Provenance

- Done: node image releases publish SBOM and provenance through Buildx.
- Include dependency and image metadata in CI outputs so release state is
  inspectable without rebuilding.

## Signing

- Done: release images and staged release assets are signed through the stack
  release workflow.
- Remaining: thread verification through more deployment docs and smoke flows.

## Reproducibility

- Done: stack release inputs are now pinned enough for deterministic rebuilds.
- Done: the release workflow now runs a post-publish rebuild audit for
  `linux/amd64` and `linux/arm64` and compares the results to the recorded
  platform digests.
- Remaining: run the hardened build path through a fresh tagged release and
  confirm the reproducibility audit is clean enough to promote from advisory to
  a hard release gate.
- Document the exact release inputs and the supported Linux/macOS operator
  paths.
