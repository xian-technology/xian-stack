# Authored contract functionality suite

`03-contract-functionality` deploys eight source contracts and executes 40 ordered
interactions (22 successes, 18 expected failures) in the default five-validator
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
| `inspector.py` | Medium | Static/dynamic ForeignHash, ForeignVariable, deterministic scans, explicit state copies/deletion, metadata reads, dynamic calls and foreign-write denial |
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

The authored suite uses the admitted syntax and implemented methods. In
particular, it does not use starred calls/unpacking, `Hash.clear/clone_from`,
`dict.update/setdefault`, `list.reverse`, or `bytes.fromhex`: these are separately
reproduced compatibility gaps in the workspace review, pending implementation and
consensus tests. Explicit per-key updates here do not close those gaps. Decimal
quantum/type, Unicode numeric parsing, repeated module bindings and same-size
dictionary key replacement during iteration also remain separate review items.
