# Rust wheel release inputs

Release builds restore these Cargo lockfiles before building any wheels. Each
directory is named for the immutable `xian-contracting` source commit in the
stack release manifest. `inputs.json` records that commit, all three lock hashes
and the provenance of the dependency selection. Unknown source commits fail
closed until their reviewed lock set is added. Ordinary development builds
continue to use the dependencies in their own checkout.

The first set restores the original v0.3.8 wheel inputs. Its published wheel
CycloneDX SBOMs record `cc 1.11.2`. The unlocked verification build selected
`cc 1.11.3`, released between image publication and verification. All dependency
name/version pairs in the compiler, VM and ZK wheel SBOMs were compared with
these lockfiles. The original image content/config verifier remains the final
acceptance gate; matching dependency lists alone is insufficient proof.

For a future source revision, generate the three wheel-root locks from a clean
checkout using the manifest's Rust toolchain, retain the lockfiles here, and
record their SHA256 hashes and source provenance in `inputs.json`. Do this
before validation/publication, then use the same reviewed files for safety,
publication and reproducibility. Never refresh an existing release's set to
newer compatible crates. The preparation helper checks the checkout SHA,
tracked-source cleanliness, exact wheel-root set, and all hashes before writing
any lock. It refuses to replace an existing different Cargo.lock.

```bash
python scripts/release_rust_inputs.py --manifest release-manifest.json \
  --contracting-root /path/to/fresh/frozen/xian-contracting
```

For historical recovery the helper comes from the recovery tooling checkout;
the Dockerfile, release manifest and verification program come from the
original immutable release tag. This restores generated build inputs without
changing its runtime source or publishing another image.
