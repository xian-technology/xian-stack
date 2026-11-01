# Authored contract functionality suite

`03-contract-functionality` deploys ten source contracts and executes 129 ordered
interactions (78 successes, 51 expected failures) in the default five-validator
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
| `surface.py` | Medium/complex | Bound mutators and callbacks, keyword signatures, range and numeric methods, live mapping views/proxies, Unicode transformations, binary mutation/codecs, deterministic time policy, field formatting and failure recovery |

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

The float-set interaction checks numeric ordering, infinities, first-inserted
signed zero, tuple/frozen-set keys, mutation, and NaN ordering and identity. It
persists strings and booleans so receipt and state checks preserve the observed
behavior without trying to persist non-finite float values.

The allowed-surface boundary flow checks pinned Unicode predicates/casing,
exception strings, ordered type checks, frozen-set copy identity, time parsing
and negative duration fields. Invalid classinfo, overflowing datetime components
and mixed format numbering each follow a pending write; the runner verifies
rollback and then a successful same-wallet recovery call. The broad per-method
matrix remains in the runtime repository rather than duplicating hundreds of
unit-level combinations as network transactions.

The completed-surface flows retrieve methods before mutating their receivers,
then invoke them directly and through `map`. They check live mapping views after
removal/insertion, numeric byte round trips, range indexing, pinned Unicode
transformations, binary aliases, explicit encodings, nested format fields and
f-string conversions with a dynamic format width.
Time expectations preserve the historical wrapper's normalized-component
duration multiplication, including a negative-duration example; numeric ISO/week
formats have explicit calendar results. Unsupported time directives, duplicate
directives, codecs, format conversions, invalid keywords and bound-method storage
each follow an authored write and event. Every failure must roll both back and is
followed by a successful same-wallet transaction through the next entry node.
Nested bound storage methods and mapping proxies are also rejected at a
cross-contract call boundary. Both failures discard pending caller writes and
events, keep the callee's tracked state unchanged, and recover with the next
ordinary call. A successful shared-list call separately verifies
that ordinary cross-contract mutation and returned-object identity remain intact.
A callee also attempts to insert its bound storage method into a shared caller
list while returning `None`. The call must fail after revalidating shared inputs;
pending writes and events from both contracts must roll back, even though the
explicit result contains no capability. The following transaction verifies
recovery.

View-set scenarios distinguish the built-in set returned by dictionary-view
algebra from the deterministic `set()` wrapper. They check equality, public type
checks, method result kinds, augmented-assignment aliases and shared cross-contract
mutation without changing kind. Explicit `set(value)` conversion is persisted and
read back in a later transaction. Raw built-in sets in storage or public results,
and mixed-kind operators in either direction, must fail after a pending write and
event. Each recovery also verifies that the previously converted set survived.
Set observations use Python 3.12.10 with the Python contracting wrappers as an
independent oracle; storage and cross-contract boundaries are checked by native
and network execution.

Host-callable flows retrieve module functions and constructors, keep a hash
callback in a dictionary, pass hash/decimal aliases through `map`, and check
datetime class checks, introspection aliases and selected-object identity through
random aliases. Nested function storage/results/cross-contract transfer and
decimal-constructor storage and datetime-constructor results must roll back
writes/events, then allow the next ordinary
transaction. The exhaustive callable catalog and signature checks remain in the
runtime suite; these network calls exercise representative compositions.
