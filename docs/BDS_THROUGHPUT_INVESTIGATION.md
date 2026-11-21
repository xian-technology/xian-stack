# BDS Throughput Investigation

## Resume point

Latest checkpoint, 2026-11-21: stack `v0.3.8`, maintenance source
`a766053c843194ba43da2867f87a29befa1834e4` on `release-0.3`.
[Image workflow](https://github.com/xian-technology/xian-stack/actions/runs/44711736245)
passed full release safety and published the images, but its later rebuild
verification failed on integrated AMD64 rootfs hashes. The final GitHub
release was skipped. Published indexes and diagnostic details are recorded in
[the release ledger](RELEASE_2026_11_17.md). Preserve these tags and original
runtime/package pins (execution 15/gas 7) while diagnosing the mismatch.
Do not resume the saturation campaign or describe the release as fully
verified yet. This is a reproducibility investigation, not another protocol
test failure; the corrected payout and leave-notice safety checks passed.

Recorded 2026-11-17. The user requested package releases and an image build
before continuing this investigation. Do not run a saturation workload against
the existing development node or replace its state as part of release work.
After the user reports image publication complete, select the newly released
immutable image digest and matching source manifest, then resume the campaign
below on an isolated network. The image workflow is to be triggered and its URL
handed to the user without active monitoring.

There is no validated current maximum TPS number and no proof that BDS can
sustain maximum chain throughput. The code identifies plausible constraints;
short correctness tests and an idle node are not saturation evidence.

## Source checkpoint before release preparation

- `xian-stack`: `e899a1d9cdcb7eb129d12f6f8935129775d67115`
- `xian-abci`: `39f57ae5cbd24268643d0def9942ec14389c5b16`
- `xian-contracting`: `83f8f4ea0977dce7f2f48f9eeb28f6b946ad5494`
- `xian-configs`: `e41c3dfb4f2853d7a99cf7f10eae1bc922bce51c`

Use the final release manifest for subsequent runs, not these pre-release
checkpoints. Current source targets `xvm-execution-15` / `xvm-gas-7`. The
running local chain reported `xvm-execution-14` / `xvm-gas-6`; its installed
image and old state must not be silently replaced with the new execution
revision. Use fresh isolated genesis/state with the selected runtime.

## What is established

- Contract execution is Rust (`xian_vm_core.BlockExecutor`); blocks execute
  transactions serially. The legacy Python executor and speculative process
  pool are gone. Simulations and contract views use Rust `execute_tx`.
- Python retains ABCI orchestration, receipts, BDS, query routing and tooling.
  The authoritative application state is LMDB. BDS/Postgres is a derived index,
  not a replacement for the authoritative node store or its full backup.
- Block packing checks the sum of transaction **supplied chi**, not merely
  charged/consumed chi. The running chain reported `max_tx_chi=200000` and
  `max_block_chi=500000` in paid-metered mode. At 10000 supplied chi per
  transaction, that permits at most 50 such transactions per block; this is
  an illustrative packing limit, not measured TPS. Oversized supplied budgets
  can obscure execution capacity.
- The running CometBFT configuration had `timeout_commit=1s`,
  `skip_timeout_commit=false`, `create_empty_blocks=true` and an empty-block
  interval of `5s`. The empty-block interval alone is not proof that busy
  blocks can only appear every five seconds. Include actual consensus timing
  and committed block timestamps in measurements.
- BDS starts one asyncio worker in the node process. It persists blocks in
  order, one PostgreSQL transaction per block. A pool maximum of 10 does not
  parallelize this writer.
- Each indexed transaction causes an insert and an address-activity upsert;
  each state change causes an insert returning its change id followed by a
  state upsert. Rewards, events, shielded outputs/tags and deployments add
  statements. Ten state changes imply at least 22 individual SQL statements
  before those extras and block overhead. This is a statement-count model,
  not a database throughput measurement.
- There is no bulk COPY/executemany persistence path in the inspected writer.
  SQL calls are individually awaited on the same connection. PostgreSQL
  indexes, JSONB/numeric projections and WAL add work per row.
- BDS enqueue occurs inside FinalizeBlock: payload JSON serialization,
  temporary-file write and rename are synchronous. This work is on the node
  critical path even though PostgreSQL persistence runs in a background task.
- The synchronous Rust `.run()` is called from the asyncio handler. Releasing
  the GIL does not yield that same event-loop thread; the BDS task cannot
  advance its Python scheduling during the call. This scheduling conclusion
  follows from the call path; its full-load impact remains to be measured.
- PostGraphile serves reads from the database; upgrading its query engine does
  not batch or accelerate the BDS writer. Source and the running container
  both reported PostGraphile 5.0.3, using `makeV4Preset` for API compatibility.

## Live observation: light load only

Read-only queries to local `http://127.0.0.1:26657` on 2026-11-17 showed:

| Observation | Value |
| --- | --- |
| Indexed height / observed chain height | 22213 / 22213 |
| Indexed transaction count | 26 |
| Height lag | 0 |
| Pending in-memory blocks | 0 / 128 capacity |
| Spool pending blocks / bytes | 0 / 0 |
| Database status | ok |
| Worker / catch-up task | running / running |
| Pool size / idle / configured maximum | 1 / 1 / 10 |
| Spool filesystem free bytes | 2977792000 (about 3 GB) |

The profiler had about 22213 finalized blocks but only 25 transaction-processing
observations. Average BDS enqueue time was about 2.538 ms per block and average
FinalizeBlock time about 5.914 ms, overwhelmingly for empty blocks. These values
cannot estimate loaded transaction capacity. Free-space figures are historical
observations: remeasure and establish headroom before load testing; do not
delete user data or test artifacts without inspecting ownership and use.

## Existing evidence and its limits

- `../xian-meta/docs/XIAN_VM_PARALLEL_EXECUTION_PLAN.md` records older
  revision-13 app-path benchmarks around 2000 transfer tx/s and 600 hot-pair
  DEX tx/s after Rust block-path improvements. They are historical,
  workload/hardware-specific results, not the current network+BDS capacity.
- `../xian-meta/docs/XIAN_SINGLE_ENGINE_PLAN.md` records later engine moves
  and parity checks. Do not reuse its historical Python-host diagnosis as a
  current bottleneck finding.
- `.artifacts/localnet-e2e/20261115T095419Z/08-throughput-mix.json` records
  successful short bursts: 160 transfer transactions, committed-window TPS
  104.587; 96 contract-heavy transactions, committed-window TPS 67.55. Only
  two blocks per measured window; the phase-wide approximate TPS was 5.571
  and includes setup/checking time. These are different metrics and none
  establishes sustained saturation. The artifact is local/ignored and may
  need recovery from the matching CI run if removed.
- The same run's `09-bds-catchup.json` shows recovery from a Postgres outage: an
  11-block spool backlog, later zero queue/spool, indexed transaction count
  increasing from 969 to 980. It verifies recovery for that test, not drain
  capacity under continuing maximum arrival rate.
- No dedicated saturation result was found under `.artifacts/tps-bench/`.

## Source navigation

Paths are relative to the sibling workspace:

- `xian-abci/src/xian/processor.py`: synchronous Rust block executor and
  conversion of native outcomes to receipts.
- `xian-contracting/packages/xian-vm-core/src/block_executor_python.rs`: the
  block loop, GIL detachment and Python outcome conversion.
- `xian-abci/src/xian/methods/finalize_block.py`: transaction result assembly,
  state-root preparation, BDS payload creation/enqueue and commit staging.
- `xian-abci/src/xian/methods/commit.py`: authoritative LMDB commit.
- `xian-abci/src/xian/state_root.py`: consensus root updates.
- `xian-abci/src/xian/fee_policy.py`: chi caps and supplied-budget packing.
- `xian-abci/src/xian/services/bds/bds.py`: worker, spool, catch-up,
  `persist_block`, `_persist_transaction`, `_load_current_state_index`.
- `xian-abci/src/xian/services/bds/database.py`: asyncpg pool and timeouts.
- `xian-abci/src/xian/services/bds/sql.py`: schema, indexes and individual SQL.
- `xian-abci/src/xian/services/bds/reindex.py`: rebuilding payloads from retained
  CometBFT blocks and recorded receipts, without contract re-execution.
- `xian-abci/src/xian/services/bds/config.py`: canonical BDS runtime defaults.
- `xian-abci/src/xian/metrics.py` and `perf.py`: current measurements.
- `xian-stack/scripts/localnet-tps-bench.py`: existing sweep/summary harness.
- `xian-stack/scripts/localnet-workload.py`: transaction workloads and committed
  window calculations.
- `xian-stack/scripts/localnet-e2e.py`: burst, mixed-load, BDS outage/recovery
  and query-pressure phases.

## Existing harness constraints

`make localnet-vm-tps-bench` initializes, **builds**, starts and finally stops
its localnet. It defaults to five validators, port offset 1000, profile
`throughput`, scenarios `transfer_fanout` and `contract_heavy`, operation counts
4000/8000/12000/16000, 64 sender wallets, 128 submit/receipt workers and a
90-second receipt timeout. The script defaults to CheckTx broadcasts and
64 heavy rounds. These are finite batch sweeps, not an offered-rate-controlled
steady-state indexer saturation test.

The throughput profile changes consensus to on-demand blocks, 500 ms propose,
200 ms prevote/precommit/commit, skip-timeout-commit enabled, and mempool/cache
sizes 50000/100000. This differs from the observed development node. Record
these choices and run the intended production-policy arm separately.

`XIAN_LOCALNET_ENABLE_BDS` defaults to false. With it enabled, localnet-init
selects one BDS validator through `XIAN_LOCALNET_BDS_NODE_INDEX` (default 0),
not every validator. Explicitly record which validator indexes and receives
read pressure. No BDS result may be inferred from the default VM sweep.

The benchmark and performance-reader paths are hardcoded under this checkout's
`.localnet/`; summaries default to `.artifacts/tps-bench/`. Use an isolated
stack checkout/project and fresh homes. After image publication, prepare that
network with the selected immutable image explicitly and run the Python sweep
against it; do not invoke the Make wrapper and accidentally rebuild a different
image. Verify the resulting container image IDs/digests and network metadata.
The harness needs extensions for steady-state arrival control, BDS backlog/rate
sampling, SQL/storage measurements and the full workload matrix described below.

## Campaign to run after image publication

1. Record the image digest, release manifest, component SHAs, execution/gas/
   admission revisions, genesis hash, fee policy, consensus/mempool settings,
   validator count/topology, hardware, Docker resource limits and disk headroom.
   Use fresh isolated homes and volumes. Do not change the existing chain.
2. Use identical genesis and offered workload for three arms: BDS disabled;
   BDS enabled without external reads; BDS plus representative PostGraphile
   and indexed-query pressure. Match hardware/resources and random seeds.
3. Ramp concurrency and offered rate through saturation, then hold a steady
   load long enough to distinguish backlog growth from startup/transient lag.
   Include transfers, hot-pair DEX, CPU-heavy contracts, mixed traffic and
   state/event-heavy transactions; test shielded indexing separately. Ensure
   the sender/client and receipt polling are not the limiting resource.
4. Record accepted, committed, successful and failed TPS separately; offered
   rate, bytes/s, state changes/s and events/s; supplied versus consumed chi;
   block utilization and actual block timing. Never report submit TPS as
   committed TPS or infer the ceiling from a small burst.
5. Collect FinalizeBlock stage times (Rust execution, decoding, receipts,
   state root and BDS enqueue), Commit/LMDB and consensus timings, process
   CPU/RSS, event-loop lag, spool bytes written and disk latency. Existing
   perf instrumentation may need a small measurement-only extension for
   commit timing and event-loop lag; do not presume they are already exposed.
6. Measure BDS transactions/rows/bytes indexed per second and per-block
   persistence latency, queue depth, height lag, signed index-height delta,
   spool count/bytes/age and free space over time. Observe PostgreSQL CPU,
   WAL, I/O, lock/wait time, query counts and read-query effects. Add timing
   instrumentation where absent. Higher pool size alone is not a writer fix.
7. Stop offered traffic and measure backlog drain time/rate. Sustained indexed
   capacity must exceed the intended committed arrival rate with operating
   margin; a bounded queue does not imply a bounded disk backlog. Define
   acceptable read staleness and recovery objectives explicitly.
8. Verify every indexed block/transaction/event/state-change identity and
   ordering against retained chain receipts, including failures, repeated keys
   within a block, rewards, typed values and shielded rows. Count equality
   alone is insufficient; compare content and checkpoints.
9. Exercise Postgres interruption/restart, indexer/node restart, interrupted
   spool writes, low-disk behavior and catch-up with continuing traffic. Audit
   fsync/power-loss durability separately: temporary-file rename is not by
   itself a power-loss guarantee. Test retention/pruning and snapshot recovery
   when source history is unavailable.

## Candidate changes after measurement

First investigate reducing database round trips with set-based/batched inserts
and upserts (possibly staging/COPY), while preserving state-change predecessor
links, per-transaction order, generated ids and block atomicity/idempotency.
Next assess a separate indexer process consuming a bounded, recoverable handoff
to isolate scheduling/CPU from ABCI. A Rust rewrite of the same row-by-row SQL
would retain much of the database round-trip cost. Only optimize whichever
measured stage actually limits the intended workload.

## Recovery and capacity cautions

The in-memory queue is measured in blocks (default 128), not transaction count
or bytes. The spool has warning thresholds (256 entries, 512 MiB and 2 GiB free
space), not an established steady-state capacity guarantee. With sustained
arrival above indexing capacity, spool storage keeps growing; chain progress
does not prove the index is caught up. Recovery needs retained local/remote
block history or a trusted BDS snapshot. Preserve independent full node-store
backups; Postgres snapshots alone do not bootstrap consensus.

## Original release handoff checkpoint (v0.3.4; failed)

Package/app publication completed in this release train; full evidence and
infrastructure recovery details are in [the release audit](RELEASE_2026_11_17.md).
The image build is triggered as stack `v0.3.4`, source
`04ca692c9e8fef104fdaa8aea45a911234f474bf`:
[image workflow; user monitors publication](https://github.com/xian-technology/xian-stack/actions/runs/44195170932). No build
status or immutable digest is asserted; the run is intentionally not watched.

The tag's release-manifest.json pins:

- ABCI 0.9.4: `620056eda8c97d5031baca75d92af045c855ce1c`
- Configs: `0c440cf65b0de407262b2c86cdd6484fb8db05cd`
- Contracting 1.1.4 and shared packages: `1534394452451267397286f8083b220dcfbb4717`
- Python SDK 0.5.3: `f1b4cbc0ac5efcad400aabf0e62b05cdbf14dc57`

The later ABCI/contracting workflow-repair commits did not change shipped runtime
code and were not substituted for these release source pins. After the user
confirms image publication, select its digest and this matching manifest before
creating the isolated fresh execution-15/gas-7 network. No saturation workload
has been run as part of this release; the live-light-load observations and
short-burst results above still do not establish sustained chain/BDS capacity.

### Image attempt reported failed: 2026-11-18

The user reported the v0.3.4 image workflow failed. It stopped in the full-E2E
bootstrap because empty Make-exported policy options were mistaken for an
explicit incompatible policy; publish-images never ran. Do not use that run
as evidence of an available image or a BDS workload result. The harness fix
uses its intended periodic/1s E2E defaults for empty options and retains the
on-demand rejection. Replacement stack v0.3.5 keeps the same component refs;
the replacement source is `ec3f43152328b8c7933c89f34a7203ccd6b27e58` and the
workflow is linked at the resume point above. Wait for the user to report
successful image publication before resuming this investigation.

### Useful evidence from the failed v0.3.5 attempt

Run 44202339923 passed phases 00–20 before the unrelated snapshot/status
failure in phase 21. Its `xian-stack-release-safety-artifacts` artifact
(ID 14385965423) contains public E2E evidence under
`public-localnet-e2e/20261118T000146Z/`.

- `09-bds-catchup.json`: baseline height 469, zero lag/queue/spool. During the
  deliberate PostgreSQL outage, the chain reached 480 and queued/spooled 11
  blocks without an enqueue error. After recovery, height 488 had zero lag,
  empty queue/spool and healthy database status. Ten catchup transactions and
  the last transaction's event/eight state writes were checked. A second BDS
  instance also resumed indexing after its outage.
- `08-throughput-mix.json`: 289 transactions across transfer-fanout and
  contract-heavy scenarios, 48.134 seconds, reported aggregate 6.004 TPS.
  This is the periodic full-E2E workload and its measurement window, not a
  saturation run or a chain/BDS capacity estimate. Do not substitute it for
  the isolated campaign planned above.

Local inspection copies: `/tmp/xian-image-v035-artifacts` and
`/tmp/xian-image-v035-failed.log`; GitHub artifact retention is finite. No image
was published by this attempt. The recovery changes only stack Make output;
it does not change the frozen ABCI, contracting, configs or SDK source pins.

### Full E2E evidence from the failed v0.3.6 attempt

Run 44406031694 passed all 37 public E2E stages, including the signed
fresh-node snapshot export/restore rehearsal. Its later, separate protocol
suite failed the fee-blind unbond payout assertion. That harness correction
is the reason for the user-selected frozen v0.3.7 maintenance release.

Artifact 14479334768 contains E2E evidence under
`public-localnet-e2e/20261118T221256Z/`. Its BDS recovery check started at
height 466 with zero lag/queue/spool, reached height 477 with 11 blocks queued
and spooled during the database outage, and recovered at height 485 with
zero lag, empty queue/spool and healthy database status. This confirms the
bounded outage rehearsal on the original runtime; it remains separate from
the planned sustained saturation and query-load campaign. Local copies are
in `/tmp/xian-image-v036-artifacts`; the protocol failure log is
`/tmp/xian-image-v036-failed.log`.

### Additional v0.3.7 evidence

Failed image run 44440254467 has public artifact 14494087408. All 37 E2E
stages passed under `public-localnet-e2e/20261119T020619Z/`; protocol proof is
under `public-localnet-protocol/20261119T024948Z/`. The corrected unbond check,
hybrid membership and real duplicate-vote evidence passed. The last protocol
stage expected eviction on leave announcement even though the chain correctly
retained the eligible announcing validator until actual exit; v0.3.8 corrects
that harness expectation without changing the runtime.

BDS again recovered from a bounded database outage: baseline height 467 had
zero lag/queue/spool; height 478 had 11 queued/spooled blocks during the
outage; recovered height 486 had zero lag, empty queue/spool and healthy
database status. This is recovery evidence, not a sustained maximum-TPS
measurement. Inspection copies are `/tmp/xian-image-v037-artifacts` and
`/tmp/xian-image-v037-failed.log`. Resume the saturation/query-load campaign
only after the user confirms publication of the selected frozen image.

### v0.3.8 verification recovery checkpoint: 2026-11-21

The original v0.3.8 safety gate and image publication passed; its final
reproducibility check failed. Read-only diagnostic run 44840683790 reproduced
the exact mismatch. Published Rust wheel SBOMs record `cc 1.11.2`; a new
`1.11.3` release between publication and verification changed the unlocked
dependency resolution. Reviewed generated Cargo locks restore the original
dependency versions without changing runtime sources, package versions,
image digests or execution/gas versions. See `RELEASE_2026_11_17.md` for the
source/provenance and investigation evidence.

The recovery workflow must still pass the original four-variant image verifier
before completing the GitHub release. Dependency-list agreement and a successful
diagnostic run are insufficient. Resume BDS saturation/query-load testing only
after that recovery is confirmed and the user resumes the investigation. The
existing live node and data remain untouched.
