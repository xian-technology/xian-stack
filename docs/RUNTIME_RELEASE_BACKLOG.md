# Runtime And Release Backlog

These items were intentionally deferred after the runtime topology cleanup.
They are the next release-engineering hardening phase, not part of the
integrated-vs-fidelity runtime refactor itself.

## Dependency And Artifact Pinning

- Done: OCI base images are now digest-pinned in Dockerfiles and release manifests.
- Done: CometBFT release builds now use a checksum-pinned source archive.
- Done: s6-overlay archives are now verified against manifest-pinned SHA256 values.
- Enforce lockfile-backed Python installs in CI and release builds.
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
- Done: the release workflow now rebuilds `linux/amd64` and `linux/arm64`
  images and compares them to the recorded platform digests.
- Document the exact release inputs and the supported Linux/macOS operator
  paths.
