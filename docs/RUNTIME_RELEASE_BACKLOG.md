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
- Done: release image `apt-get update` runs against a manifest-pinned Debian
  snapshot instead of live Debian repositories.
- Done: the uv helper image and the Python packaging build dependency are now
  manifest-pinned release inputs.
- Added: reviewed Cargo lock sets for the three native Python wheel roots,
  keyed by the immutable contracting source SHA. Release safety, publication
  and verification restore the same files. Unknown source pins fail closed.

## SBOM And Provenance

- Done: node image releases publish SBOM and provenance through Buildx.
- Include dependency and image metadata in CI outputs so release state is
  inspectable without rebuilding.

## Signing

- Done: release images and staged release assets are signed through the stack
  release workflow.
- Remaining: thread verification through more deployment docs and smoke flows.

## Reproducibility

- Base images, uv, Debian packages, Python tools, CometBFT and s6-overlay are
  pinned. v0.3.8 exposed a remaining gap: untracked Cargo locks allowed a new
  transitive crate between publication and verification. Restored Rust locks
  now cover that input; full original-image verification remains the acceptance
  check before calling the existing release reproducible.
- Done: the release workflow now runs a post-publish rebuild audit for
  `linux/amd64` and `linux/arm64` and compares the results to the recorded
  platform digests.
- Done: the release workflow now treats reproducibility as a hard GitHub release
  gate; images are still built and published first, but the GitHub release is
  blocked unless the rebuild audit passes.
- Done: supported Linux/macOS operator verification paths are documented in
  `docs/RELEASES.md`.
- Remaining operational check: run the next tagged release through the hardened
  path and confirm the release audit is clean with real published image digests.
