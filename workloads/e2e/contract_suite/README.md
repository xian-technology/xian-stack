# Authored contract functionality suite

`03-contract-functionality` deploys eleven source contracts and executes 212 ordered
interactions (126 successes, 86 expected failures) in the default five-validator
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
| `followups.py` | Medium/complex | Lexical locals and comprehension isolation, hash/crypto byte costs and limits, typed wrapper-set persistence/query/readback, mutable interface descriptor identity/enforcement, callable-bearing builtin view sets and storage boundaries |

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
mutation without changing kind. Explicit `set(value)` conversion is checked,
then a sorted member list is persisted and reconstructed in a later transaction.
Current nodes admit typed wrapper sets through the versioned set codec. The
followup flows below verify their persisted representation and reconstruction.
Raw built-in sets in storage or public results,
and mixed-kind operators in either direction, must fail after a pending write and
event. Each recovery also verifies that the saved member list survived.
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


The followup flows persist both mutable and frozen wrapper sets containing a
nested tuple, frozen set and raw Decimal. All validators must return the exact
versioned typed encoding from storage queries; a later transaction checks the
reconstructed kinds, decimal quantum and detached read mutation. These checks
complement the codec's exhaustive boundary fixtures.

Lexical failures include reads before assignment, comprehension target isolation,
and an unbound callable or storage receiver whose argument would raise a distinct
error if evaluated too soon. Every failure rolls back a pending write and event,
then the same wallet submits a successful recovery. Mutable Func/Var descriptors
exercise shared argument identity, name/argument updates, `is_of`, interface
enforcement and identity-key lookup. Descriptor storage and constructing anonymous ORM storage through descriptor
metadata remain rejected. Audited unbound read and mutation methods are tested
separately against both writable local and read-only foreign receivers.

Callable-bearing builtin view sets check receiver identity, deduplication,
membership, stable encounter ordering and invoking the remaining bound method.
They cannot be converted into deterministic wrapper sets or persisted. The
separate runtime-safety phase likewise rejects direct and nested builtin view
sets; valid wrapper persistence is now covered by the positive followup flows.

Hash calls use a 16KiB UTF-8 value through direct, aliased and callback routes.
A constrained-budget callback transaction rolls back, followed by the identical
work succeeding with a sufficient budget. Oversized Ed25519 key/signature inputs
exercise the small network-safe admission limits. Full 1MiB message/hash input
caps, metering-off behavior and exact per-byte cost deltas remain in native
resource tests instead of inflating network transaction payloads.

The later BDS catch-up phase recovers the authored contract name from its saved
phase report and writes a different tuple label while PostgreSQL is offline.
After spool recovery, current state and that transaction's state-change rows must
contain the exact typed envelopes with null numeric projections. The secondary
indexer must preserve the same values during initial RPC reindexing and after its
own restart/resume. These are live PostgreSQL assertions; helper tests also prove
that missing rows, flattened members, lost decimal quantum and stale transaction
IDs fail the checks.


The API adapter flows invoke event helpers directly, through a local alias and
through the recovered event class. They verify indexed integer schemas, a
1024-byte UTF-8 value at the event limit, wrong-type rejection and oversized-value
rollback. A nested event helper call checks declaring-contract attribution,
caller/signer identity and restoration of the original caller frame.

Unbound Variable/Hash methods read and mutate local state and clone a foreign
source into local storage. Wrong receiver families and writes through foreign
Variable/Hash receivers fail, including attempts to clear or clone into a foreign
receiver. Each failure is followed by a successful transaction.

Imported public aliases exercise an admitted host helper, hashing function,
builtin constructor, class-bound bytes factory and exported contract function.
The exported alias retains its original contract frame. Private functions,
storage handles/methods, events, constants and module namespaces remain
inaccessible through this callable-only import surface. Rejected callable
lookups are checked before their deliberately failing argument expressions.
Interface checks also verify that missing/None members return false and ordinary
constants return true without invoking a noncallable `is_of` override; an actual
function member requires a callable check.

`Contract.submit` remains restricted to the canonical submission context.
Ordinary contracts attempt both bound and unbound helper calls. A contract
submitted through the canonical factory also attempts another helper deployment
from its constructor. Each denial rolls back writes/events and the following
transaction verifies that no child or nested child remains. Successful helper
adapter calls inside the submission context are covered by focused runtime tests;
the surrounding orchestration phase covers successful canonical deployments.


Custom `Func.is_of` and `Var.is_of` predicates receive immutable, execution-local
metadata subjects. The suite exercises both accepted and rejected interfaces,
wrapping the default matchers and persisting only the subjects' scalar metadata.
It verifies canonical function names/arguments and storage family names, including
private-function metadata, while callbacks remain in the inspecting contract's
caller/signer frame. No target function or storage handle is exposed.

Failures try to mutate metadata, invoke a private-function subject, access storage
through a subject, and hide subjects in nested persistent values, public results
or cross-contract arguments. A callback also writes state and emits an event
before raising. All these calls must roll back, preserve the inspected contract's
state, and allow the next ordinary transaction.

A final interface check selects its exported predicate through
`importlib.import_module(ctx.this)`. It compares caller, signer, owner and contract
identity before, inside and after the callback, ensuring a local module lookup
keeps the inspection frame instead of becoming a cross-contract self-call.
