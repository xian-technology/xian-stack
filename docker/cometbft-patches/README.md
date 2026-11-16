# Verified evidence history

The release manifest pins the official CometBFT 0.40.0 source archive and this
patch separately. `docker/prepare-cometbft-source.sh` checks both SHA-256 values
before extraction, applies the patch to a temporary tree, normalizes the release
version and records the build inputs. The Docker build tests all affected Go
packages before producing the binary.

This backend is for a fresh chain with the matching Xian application. It is
consensus-sensitive; do not install it as a rolling upgrade on an existing
chain. See the operator compatibility and snapshot guides in `xian-docs-web`.

Snapshot bootstrap verifies the complete retained evidence window before
catch-up or voting. Its evidence-only history lives in `evidence.db`; an
independent pending/ready journal in `state.db` requires that history on restart.
Neither database is a disposable performance cache. Historical validator copies
do not supply current proposer priorities, and `/evidence_history` supplies no
block bodies. The ordinary light-client trust root and independent trusted RPC
providers remain operator responsibilities.

The patch contains its regression tests. Changes require a new patch checksum,
the matching application/protocol checks, and the combined snapshot recovery,
pre-checkpoint evidence, replay and multi-node release gates. Release tooling
embeds the upstream commit, patch digest and history protocol revision in the
binary/image provenance; configured manifest metadata alone does not attest a
running binary.
