# Runtime And Release Backlog

These items were intentionally deferred after the runtime topology cleanup.
They are the next release-engineering hardening phase, not part of the
integrated-vs-fidelity runtime refactor itself.

## Dependency And Artifact Pinning

- Pin OCI base images by digest in Dockerfiles and release manifests.
- Keep CometBFT downloads versioned and checksum-verified for every target
  architecture.
- Enforce lockfile-backed Python installs in CI and release builds.
- Audit any remaining floating runtime dependencies and either pin them or
  document why they intentionally float.

## SBOM And Provenance

- Generate SBOMs for node images and publish them with releases.
- Attach build provenance or attestations to release artifacts.
- Include dependency and image metadata in CI outputs so release state is
  inspectable without rebuilding.

## Signing

- Sign release images and published artifacts.
- Verify signatures in deployment documentation and CI smoke flows.
- Define a single release-signing path instead of ad hoc per-repo handling.

## Reproducibility

- Make release builds reproducible from tagged inputs, lockfiles, and pinned
  external artifacts.
- Add a CI check that rebuilds release candidates and verifies digests where
  practical.
- Document the exact release inputs and the supported Linux/macOS operator
  paths.
