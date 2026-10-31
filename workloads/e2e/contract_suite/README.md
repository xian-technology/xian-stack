# Authored contract functionality suite

`03-contract-functionality` deploys nine source contracts and executes 73 ordered
interactions (44 successes, 29 expected failures) in the default five-validator
E2E run. Run `make localnet-e2e` from `xian-stack`; run the fast native equivalent
with `uv run pytest tests/test_contract_suite.py`.

| Contract | Level | Exercised behavior |
| --- | --- | --- |
| `counter.py` | Easy | Constructor arguments/defaults, Variable/Hash writes, tuple keys, explicit deletion, opt-in argument checking, authorization, events, rollback and recovery |
| `collections.py` | Medium | Arithmetic, exact decimals, rounding, comparisons, bits, sequence/dictionary comprehensions, branches, loops with break/continue/else, unpacking, recursion, variadic/keyword calls, container aliasing, sorting, iterators, sets and builtin constructors |
| `codec.py` | Medium | String methods/slices, Unicode helpers, number formatting, bytes/bytearray, SHA-256/SHA3 explicit encodings and historical aliases |
| `ledger.py` | Medium | Exact-decimal balances and allowances, multiple actors, private helpers, immediate caller/original signer/contract identity, transfer events |
| `escrow.py` | Complex | Static imports, delegated funding, deadlines, buyer authorization, settlement/refund/replay prevention, rollback after nested transfer and emitted events |
| `router.py` | Complex | Dynamic imports, export discovery, strict zip, atomic batch payments, second-call failure undoing the first transfer, private-function denial and recovery |
| `inspector.py` | Medium | Static/dynamic ForeignHash, ForeignVariable, deterministic scans, snapshot cloning, bulk deletion, metadata reads, dynamic calls and foreign-write denial |
| `compatibility.py` | Medium | Ordered module bindings, starred calls/unpacking, streaming dictionary methods, in-place reverse, hex decoding, dictionary mutation/compaction, Unicode numbers, raw Decimal quantum/type and all 53 public method names, fractional powers, exact float construction, DecimalTuple, NaN identity, non-finite transport/storage rejection, typed storage, prefix clears, self-cloning, rollback and recovery |
| `commitments.py` | Medium | Ed25519 key/signature validation, application nonces, tampering/replay rejection, datetime parsing/arithmetic and deterministic random bridge invariants |

Sources are ordinary authored contracts with the ledger import name substituted
before deployment. `scripts/contract_suite_cases.py` supplies explicit expected
returns, events and state transitions to both native and network tests. It is
not a second implementation of the contract business logic.

For every network call the runner checks the expected result or failure reason,
positive chi billing, exact ordered application events, identical complete
transaction results on all five validators, and every tracked application state
key on all five validators. Failed calls must retain no authored-contract writes
or events; fee-accounting writes are allowed. Submission entry nodes rotate.
Native failures additionally require an empty write set. Constructors are checked
before calls. Fresh contract names make a phase retry start from clean application
state; the phase report retains names, actors, transaction hashes and heights.

An AST inventory requires a direct use of every admitted callable builtin. This
is nominal API coverage, **not** every input combination or Python compatibility.
Random tests check bounds/permutations and singleton selection, rather than
assuming a PRNG sequence independent of transaction context. The full E2E replay
compares transaction results, chi, events and application roots for all blocks,
including these deployments and calls.

## Complementary coverage and limits

The surrounding E2E phases cover nested deployment, ownership/developer metadata,
constructor budgets, pending overlays, persistence/restarts, resource limits,
shielded proofs, signature adversaries, governance, DEX recovery, dictionary
identity/equality, lazy/live iterator behavior, and node/indexer failure recovery.
The runtime's native and pinned historical compatibility suites remain necessary.

All seven R10–R16 compatibility items have explicit network scenarios. The suite
also checks foreign-source cloning in the inspector and common method calls in
the collection/codec fixtures. The pinned historical and native suites add
argument-error, boundary, allocation and metering cases beyond these network
flows. R17 interactions cover general fractional powers, all 53 publicly reachable
Decimal method names, exact `from_float`, DecimalTuple fields and sequence behavior,
NaN identity versus numeric equality, and non-finite values passed through a
nested contract call. Results use strings and booleans to preserve special-value
semantics across receipt transport. Four direct/nested Variable/Hash failures
verify non-finite persistence rejection and rollback, each followed immediately
by a successful write from the same wallet on the next entry node. Method and
power expectations come from historical contracting revision `3b82001f` under
Python 3.12.10. This is nominal method coverage, not every argument or resource
boundary; the historical/native suites provide those deeper cases.
