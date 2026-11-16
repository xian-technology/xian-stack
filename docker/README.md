# Docker

## Purpose
- This folder contains the image definitions used by the Xian runtime stack.

## Contents
- node image definitions
- PostGraphile image support
- `s6-overlay` support assets

## Notes
- Keep runtime-image changes aligned with release manifests and smoke coverage.


## Upstream CometBFT and store inspection

The node image builds the checksum-pinned official CometBFT 0.40.0 source archive
without applying patches or editing upstream files. Its provenance is installed
at `/usr/local/share/xian/cometbft-provenance.json`; the image labels
`org.xian.cometbft.version` and `org.xian.cometbft.source.commit` identify the
intended release and exact source. This upstream tag leaves its fallback version
constant at `0.39.0`; the unmodified binary therefore reports
`0.39.0+0880b4d378f347ab16e54ec677ff50d803f37d62`. Use the source commitment and
archive checksum when identifying the build.

The separate [`node-store-inspect`](node-store-inspect/README.md) utility links
against those official libraries and is tested during image construction. It
checks stopped native stores before a full-store archive is published or
restored; it does not change CometBFT consensus or database behavior. The Python
verifier additionally binds the app checkpoint and trusted signed manifest.
