## Cross-Repo Review

Date: 2026-03-21

Scope reviewed:
- `xian-abci`
- `xian-contracting`
- `xian-py`
- `xian-cli`
- `xian-stack`

Primary focus:
- consensus correctness
- parallel transaction execution
- Python 3.14-sensitive code paths
- runtime recovery and indexing paths

### High Severity

1. Proposal validation is effectively disabled.
- `xian-abci/src/xian/methods/prepare_proposal.py`
- `xian-abci/src/xian/methods/process_proposal.py`
- `xian-abci/src/xian/methods/finalize_block.py`
- `xian-abci/src/xian/methods/check_tx.py`
- `xian-abci/src/xian/utils/tx.py`
- Current state: proposer transactions can bypass mempool-side validation because proposal handling accepts everything and finalization does not rerun signature / chain-id / nonce validation.
- Next action: add deterministic consensus-path validation for proposal and finalization, using block-local nonce sequencing instead of node-local mempool pending nonces.
- Status: fixed on `main` in this review pass.

2. SDK silently corrupts decimal values.
- `xian-py/src/xian_py/xian_async.py`
- Current state: balances and state values are coerced through Python `float`, and `send` / `approve` convert user amounts to `float` before broadcast.
- Impact: high-precision contract values are truncated or rounded incorrectly.
- Next action: remove float coercion from the SDK and preserve canonical decimal strings / `ContractingDecimal`-compatible values end-to-end.
- Status: fixed on `main` in this review pass.

3. `xian-py` decompiler is broken on Python 3.14.
- `xian-py/src/xian_py/decompiler.py`
- Current state: still uses removed AST node aliases such as `ast.Str`, and also converts decimal literals through `float`.
- Impact: `get_contract(clean=True)` is currently broken on Python 3.14 and loses precision.
- Next action: port decompiler transforms to modern AST nodes and keep decimal literals as strings / exact decimal syntax.
- Status: fixed on `main` in this review pass.

4. BDS can still backpressure block finalization.
- `xian-abci/src/xian/methods/finalize_block.py`
- `xian-abci/src/xian/services/bds/bds.py`
- Current state: finalized blocks await `enqueue_block()`, which writes spool files inline and can block on a full in-memory queue.
- Impact: database outage or slow indexing can still affect the validator hot path.
- Next action: decouple enqueue from the consensus path more completely, or add a guaranteed non-blocking local spool write path with bounded failure semantics.
- Status: partially fixed on `main` in this review pass.
  - Queue-full backpressure no longer blocks `FinalizeBlock`.
  - BDS enqueue failures no longer fail the block path.
  - Remaining concern: spool-file disk I/O still happens inline, and disk-full on the spool path is still operationally relevant.

### Medium Severity

1. Pending nonces can strand senders until commit.
- `xian-abci/src/xian/nonce.py`
- `xian-abci/src/xian/methods/check_tx.py`
- `xian-abci/src/xian/methods/commit.py`
- Current state: pending nonces are advanced at `CheckTx` time and only cleared on commit.
- Impact: dropped or evicted transactions can leave a sender blocked on an on-demand chain.
- Next action: add mempool eviction / timeout reconciliation or move to a tighter proposer-aware nonce reservation model.

2. Query / SDK type contract is ambiguous.
- `xian-abci/src/xian/methods/query.py`
- `xian-py/src/xian_py/xian_async.py`
- Current state: ABCI query `info` is too weak and the SDK guesses result types heuristically.
- Impact: string values that look numeric or JSON-like can be mis-decoded.
- Next action: define a typed query response contract and consume it explicitly in the SDK.
- Status: fixed on `main` in this review pass for the current node/SDK surface.

3. `simulate_tx` falls back to local wall clock when the chain is idle.
- `xian-abci/src/xian/simulator.py`
- Current state: when there is no current block meta, simulation uses local `datetime.now()`.
- Impact: different nodes can simulate the same transaction under different `now` values.
- Next action: define a stricter idle-chain simulation policy and expose it clearly.
- Status: fixed on `main` in this review pass.
  - Simulation now uses the latest committed chain time from local block metadata.
  - If no chain time exists yet, it falls back to deterministic epoch time instead of wall clock.

4. BDS snapshot extraction is less hardened than state-sync snapshot extraction.
- `xian-abci/src/xian/services/bds/snapshot.py`
- Current state: path checks exist, but extraction should use the safer filtered extraction path consistently.
- Next action: align BDS snapshot extraction with the state-sync safety path.

5. Parallel execution still pays high per-block startup cost.
- `xian-abci/src/xian/parallel_executor.py`
- Current state: a new process pool and fresh client/processor objects are created for each block / speculative task batch.
- Impact: contract-engine speedups do not fully translate to network TPS.
- Next action: investigate persistent workers and cheaper worker initialization.

### Low Severity / Structural

1. Localnet hardcodes periodic empty blocks.
- `xian-stack/scripts/localnet-init.py`
- Current state: localnet sets `create_empty_blocks = true` and a `5s` interval.
- Impact: can hide on-demand time / idle-chain bugs.
- Next action: make localnet block policy explicit per scenario and default it deliberately.

2. CLI snapshot restore still uses the old full-home tarball path.
- `xian-cli/src/xian_cli/cli.py`
- `xian-abci/src/xian/node_admin.py`
- Current state: the CLI restore workflow does not yet prefer the newer canonical app-state snapshot path.
- Next action: unify snapshot UX around the new state-sync snapshot model.

3. Standalone contract submission still uses naive local time in one path.
- `xian-contracting/src/contracting/storage/driver.py`
- Current state: fallback contract submission timestamp still uses local wall time.
- Impact: not a consensus issue, but inconsistent with the rest of the stack’s UTC / block-time model.

### Current Execution Order

1. Revisit nonce pending-state behavior.
2. Harden BDS snapshot extraction path.
3. Reduce parallel executor startup cost.
4. Decide whether to further decouple BDS spool writes from the validator path.
