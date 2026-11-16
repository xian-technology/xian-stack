# Native store inspector

`xian-node-store-inspect` is a separate, read-only verification tool linked to
unmodified upstream CometBFT. It does not change CometBFT's consensus, storage,
state sync, or evidence processing. The image build compiles this directory
against its pinned upstream source and ships the binary beside `cometbft`.

Run on a stopped node's staged database copy. The exporter's optional lock
lease may inspect the stopped source while making that copy:

```bash
xian-node-store-inspect --db-dir /staged/data --db-backend goleveldb \
  --genesis-file /trusted/genesis.json
```

The tool requires existing GoLevelDB directories and regular `LOCK` files for
`state`, `blockstore`, and `evidence`. It uses the native read-only shared locks,
strict checksum validation and `ErrorIfMissing`; it never creates or repairs a
store. A running native writer prevents inspection. Other backends fail with an
explicit unsupported-backend error; ordinary replay is unaffected.

Successful output is one JSON object. `schema_version` is integer `1`; all
heights, byte counts, durations and nanoseconds are canonical decimal strings.
Hashes are 32-byte lowercase hexadecimal strings:

- `chain_id`, `height`, `initial_height`, `app_hash`, `block_hash`,
  `last_block_nanos` describe the native committed checkpoint. `app_hash` is
  state H's root; header H contains the previous state's root.
- `evidence` contains the complete `max_age_num_blocks`, `max_age_duration`
  (nanoseconds), and `max_bytes` triple.
- `block_store_base` and `block_store_height` describe body retention.
- `history_first_verified` and `history_first_nanos` describe the lowest
  authenticated header inspected, including an expired boundary or the chain's
  initial height.

The scan authenticates the header hash chain from the checkpoint and verifies
every live historical header, commit signatures/hash/full block ID, and validator
set. Commit hashes omit the round, so historical signatures must also verify.
The strict upstream `VerifyCommit` checks all non-absent signatures, including
NIL votes, while requiring more than two thirds of block voting power.
Checkpoint verification uses the seen commit first, then the canonical commit,
as consensus restart does. Historical evidence requires the canonical commit;
a retained seen commit cannot replace that required record. Expiry requires
**both** a strictly greater block age and a strictly greater duration. It stops
only at an authenticated expired header or the initial height. Retained native
header/commit records below the body base are supported. Missing live material
or a missing boundary fails closed. An expired boundary's commit and validator
records are not required.

Every advertised retained body from `block_store_base` through the checkpoint
must exist, contain valid Merkle-proven parts, and match its full block ID and
size. Bodies below that base may be pruned. The native cached GenesisDoc must
match the required trusted `--genesis-file` after official parsing and semantic
JSON normalization; matching the chain ID alone is insufficient.

`--max-history-heights` defaults to `1000000` and separately bounds live headers,
retained bodies and evidence database records. `--timeout` defaults to `2m`. Operators may explicitly raise these local
inspection budgets for larger retained stores. Exceeding either budget returns
an error and no successful result; these limits are not consensus rules.

`--hold-open` emits the result and retains all three read-only native locks until
stdin EOF or the timeout. The exporter uses this lease while copying the source.
The only accepted input is EOF; bytes on stdin terminate the lease with an error.
The caller must wait for a successful process exit before publishing its copy.

The Python verifier additionally compares this report with the staged app's
committed marker/root/evidence policy, local trusted genesis, and signed snapshot
manifest. The trusted signer and archive checksum authenticate the backup's
contents. This is checkpoint consistency and retained-evidence verification; it
is not an independent replay of the chain or a proof of every application state
transition.

For focused tests, copy this directory under `xian-tools/node-store-inspect` in a
fresh extraction of the pinned upstream module, preserving every upstream file:

```bash
GOMAXPROCS=2 go test -p 2 ./xian-tools/node-store-inspect
```
