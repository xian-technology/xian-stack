# Historical Deployed contract authorization audit and upgrade plan

This document records the earlier in-place upgrade investigation. The accepted
launch now uses fresh contracts and balance-only migration. The old source-patch
rehearsal script has been retired along with the permit authorizer; commands and
patch candidates below are historical evidence, not current operator procedures.
Use [snapshot migration](../../xian-meta/docs/SNAPSHOT_GENESIS_MIGRATION.md) and
`scripts/rehearse_balance_import.py` for the new-chain workflow.


Enrollment remains disabled. This plan covers the existing five-validator
**local** `xian-localnet-1` deployment, not historical/public networks or an
independent security audit. No live transactions, votes, patches or policy
changes were submitted during the audit.

## Applicability to the new-chain launch

The launch direction is a fresh chain with migrated funds in a new genesis,
followed by a separately designed contract. `team_lock` is not required.
This document records the existing localnet and a tested **in-place** upgrade
option; deploying that patch or recreating its missing lock is not a prerequisite
for the fresh chain. Use guarded sources directly in the new genesis and audit
that genesis and subsequent contracts. Replace the obsolete draft allocations
and initial metadata authority through the accepted migration design.
See the [fresh-chain launch scope](../../xian-meta/docs/MAINNET_LAUNCH_PLAN.md#fresh-chain-migration-scope).
Enrollment remains disabled pending the remaining release and custody checks.

## Evidence and findings

The 2026-10-12 RPC inventory at height **126587** captured all **24 contracts**,
including source, executable IR and **442 contract storage entries**, identically
from ports 27657, 27757, 27857, 27957 and 28057 on localhost. `_accounts` has no
entries and its policy is absent. All 24 deployed sources reproduce their stored
IR with the current compiler (comparison of parsed canonical IR).

Inventory SHA-256:
`183b8910b8dc54b03b75de75a1d142a60c358e5a5f8525c5aaa701a54fc51a28`.
Local evidence is under `.artifacts/deployed-contract-audit/20261012-verified-3/`.
`inventory.json` includes every source/IR hash and exact typed RPC envelope;
`report.json` contains navigation hints, not security approvals.

A read-only LMDB transaction at height **126603** exported **451 typed state
entries**, including nonce/runtime records. Every contract key and its RPC
representation matched the earlier multi-validator capture. RPC JSON itself
loses nested runtime types and must not be used as a restorable state backup.
Typed export SHA-256:
`7cd70036332600b9970f5867bd7f17923d918f5c812f57e8a0014c27cdacdb40`.
Neither this state export nor the RPC inventory replaces a full operator backup
of CometBFT and application data.

### Blocking legacy permission path

The deployed `permit_authorizer.permit` validates an owner's original Ed25519
signature and calls `currency.approve_from_authorizer`. Neither deployed entrypoint
checks the registry. A relayer with an enrolled owner's old key could therefore
create a spending allowance. This bypass was reproduced with a **disposable key
in an isolated copy** of deployed state. No actual user's key was used.

The proposed change guards both entrypoints with a read-only `_accounts.records`
lookup. Any existing record rejects legacy delegated approval; enrolled users
approve directly through authenticated transactions. The currency guard also
blocks a stale replacement authorizer selected later. Generic `crypto.verify`
remains a stateless primitive; this change does not redefine all contract crypto.

| Contract | Deployed source SHA-256 | Proposed normalized source SHA-256 |
| --- | --- | --- |
| `permit_authorizer` | `e06fed8d8db7d52f7d87050b25900771c05bccc059d2c245aee354d759583913` | `b34dbcdf4f0f58624cf4da17b7c4d1285dc557a346f545b01cb86c09de136e6e` |
| `currency` | `a4a91941e831ebd54ea25180ea5cd2e00d8e75f2add43d69d84ea7cbe20cd981` | `dc6819c66ab2ab4657e7d42b7e2d1bd0072c1cff3b929a28b3871433a3a839f6` |

The semantic source diff contains only the two registry bindings and guards.
Constructors are not re-executed. The proposed IR hashes and allowed storage
changes are recorded in the rehearsal artifact.

### Complete deployed inventory review

These are source-reviewed permission paths, not a certification of each
contract's entire business logic. Dynamic callees and their state were included;
the token factory's embedded generated source was inspected as well.

| Deployed contracts | Authorization path and required treatment |
| --- | --- |
| `permit_authorizer`, `currency` | Patch both legacy approval entrypoints before enrollment. `currency.metadata:permit_authorizer` currently points to `permit_authorizer`; preserve that pointer. |
| `con_brl024bd7`, `con_brl7609b6` | Bridge minter/operator compare `ctx.caller`; transfer/approve bind the caller. Preserve original identities; migrate service custody and independently review bridge authority. |
| `con_cons_collateral`, `con_cons_reserve`, `con_cons_stable` | Governor/controller checks use caller identity; `transfer_from` consumes persistent approvals. The stable token permits `con_cons_psm` and `con_cons_vaults` as controllers. |
| `con_cons_psm` | Governor controls parameters; swaps use token approvals and the stable controller privilege. Preserve configured contracts and treasury. |
| `con_cons_savings` | Caller owns deposits/shares; allowances permit delegated share transfers. Preserve the token and governor references. |
| `con_cons_vaults` | Caller owns vault operations; governor configures collateral/oracle/savings; liquidation and auction settlement have deliberate public entrypoints. Preserve vault ownership, debt, collateral and auction state. |
| `con_cons_oracle` | Governor manages reporters; reporters authenticate as callers. Original governor remains a reporter unless separately removed. |
| `con_cons_nft` | Owner/token approval/operator approval authorize transfers and burns. Stored listings authorize later purchases; content creator and collection operator retain rights. Review all these delegations after recovery. |
| `con_xsc005` | Interface and foreign metadata reads; no spending or signature authority. |
| `token_factory` | Submission of embedded token source; initial ownership defaults to authenticated `ctx.signer`. Generated token uses caller/operator and allowance checks, no legacy signature relay. |
| `submission` | Runtime owner/developer changes require the current caller's authority. Does not provide a normal redeploy-over-existing-name mechanism. |
| `governance`, `validators` | Caller/member and snapshotted voting-weight checks. Governed patches are powerful enough to change authorization state; governance account custody and consensus security remain separate gates. |
| `chi_cost`, `rewards`, `dao` | Controller is `validators`; DAO calls token contracts under its own identity. Preserve that authorization chain. |
| `foundation` | Owner authenticates as caller. Migrate the owner's custody without rewriting its address. |
| `zk_registry` | Owner is `governance`; ownership transfer requires pending owner's caller identity. Groth16 proof security is a separate boundary. |
| `con_consistency_probe_typed` | Public test storage, no fund authority. Keep out of a public launch fixture unless deliberately required. |
| `con_foreign_probe` | Test contract contains an attempted foreign write; safety depends on the VM's read-only foreign-storage enforcement. Require that runtime regression gate; source scanning cannot certify it. |

No stream-payment contract is deployed in this inventory. The guarded stream
source in `xian-contracts` still needs an upgrade wherever other deployments or
copies exist. Do not infer their safety from this localnet inventory.

All captured numeric token allowances are zero and no NFT operator-approval
entries were present. Preserve them in this source patch, but recheck at the
actual activation height. Existing delegation is not revoked by enrollment or
recovery. A spender, bridge minter, reporter, governor, or controller can still
exercise its assigned authority using its own credentials. Users/operators
must deliberately retain, revoke or migrate those authorities.

### Existing account-access issue: missing team_lock

`currency.metadata:operator` is `team_lock`, and
`currency.balances:team_lock` is **66,666,666.6 localnet XIAN**, but `team_lock`
is absent from the complete deployed contract list. That named contract cannot
currently make its authorized calls. This predates the hybrid migration and is
not fixed by changing signing keys.

This finding does not require recreating `team_lock` on the new chain. If this
existing deployment is used as a production-like access rehearsal, reconcile
the intended allocation/custody design. A separate reviewed governed deployment
of the intended lock at the same name may preserve the balance and operator
identity, but its owners, release rules, initialization and recovery must be
explicitly agreed and tested. Do not silently redirect the funds, manufacture a
replacement owner or include unrelated allocation changes in this patch.

## Optional in-place state-preserving upgrade sequence

1. Keep `_accounts.policy` absent for this network. If a later network already
   has enrolled accounts, retain all records and the policy and set only
   `enrollment_open=false`; never delete registry state or downgrade validators.
2. Pin node/compiler/SDK/wallet releases and image digests. Confirm registry
   support and foreign read restrictions on every validator/RPC. Rerun the
   inventory on the exact target, including new deployments, aliases, embedded
   sources, governance proposals and scheduled patches. Missing or changed
   evidence blocks activation. Hash equality does not replace manual review.
3. Take and restore-test operator backups. Review existing token approvals, NFT
   operators/listings, bridge custody and service/governance authorities with
   the responsible owners. Preserve balances, nonces, ownership, vaults and
   delegations unless a separately authorized change specifies otherwise.
4. Prepare one governed **source patch** with exactly
   `currency.__source__` and `permit_authorizer.__source__`. Keep the same
   contract names and all storage. Do not call constructors, redeploy under new
   names, copy balances or directly edit derived `__xian_ir_v1__` values.
   The node compiles both sources into matching IR at application time.
5. The generated `draft-patch.json` has `activation_height: null` deliberately
   and is not loadable. After final review, select a future height, include the
   explicit chain ID, and compute the bundle hash with the release's
   `StatePatchManager`. The deployed governance has a normal minimum delay of
   20 blocks and a 4/5 approval-weight threshold; read the live member weights
   and threshold again when preparing the proposal. Do not assume validator
   count equals governance voting weight or use the emergency path by default.
6. Distribute identical approved bundle bytes to every validator's
   `<COMETBFT_HOME>/config/state-patches/` and verify `/state_patch_bundles`
   inventory before scheduling. Nodes load bundles at startup; loading a file
   into a running directory is insufficient. Rehearse staged restarts and
   missing/mismatched bundles in a disposable network first. Use the existing
   governance `propose_state_patch`/`vote` path only after review and distribution.
7. At activation, verify patch status/execution hash, source and IR hashes,
   chain convergence and exact expected storage delta. Compare against the
   pre-activation state, accounting explicitly for legitimate block transactions,
   fees and rewards. No broad state-root equality is expected across blocks.
   Non-artifact changes should be normal block effects and patch bookkeeping.
8. On an isolated copy of final deployed state, rerun enrollment/recovery and
   test old-key permit rejection, direct approval/spending, owner-restricted
   calls, delegation revocation, nonce preservation and restart. Repeat the
   actual governed upgrade with five validators from pinned images, including
   crash/replay and missing-bundle failure. The offline approval fixture below
   is not evidence that those governance/distribution steps have been exercised.
9. Resolve the missing-lock allocation issue and all independent-review,
   physical-device recovery, performance/DoS and coordinated-release gates.
   Enrollment activation must be a **separate** reviewed decision. The source
   patch and these tools do not enable it.

If an upgrade fails, keep enrollment closed and repair forward. Do not restore
old permit code, remove records or rewind past enrolled-account transitions.
Pause dependent services and preserve pending credentials until the committed
state is reconciled. A fresh future patch requires fresh review and hashes.

An unrestricted third-party deployment can introduce a new legacy signature
relay later. Inspect dependencies whenever users grant permissions to contracts;
this inventory is valid for its captured state only. Neither the inventory nor
these guards limit malicious governance or establish whole-chain quantum safety.

## Reproduce the audit and offline rehearsal

From `xian-stack`, collect a new directory (existing output is rejected):

```bash
uv run python scripts/deployed_contract_audit.py \
  --chain-id xian-localnet-1 \
  --rpc http://127.0.0.1:27657 --rpc http://127.0.0.1:27757 \
  --rpc http://127.0.0.1:27857 --rpc http://127.0.0.1:27957 \
  --rpc http://127.0.0.1:28057 \
  --output .artifacts/deployed-contract-audit/new-inventory
```

Collection success means complete agreeing RPC evidence, **not approval to
enroll**. Every report sets `enrollment_ready=false`. Queries demand one committed
height; if the chain advances mid-capture, the run fails instead of combining
states. Retry into a fresh directory or use a coordinated snapshot. These RPCs
have no state proofs; validate endpoint/operator identity separately.

For a local LMDB rehearsal, export public state using one read transaction with
`lmdb.open(path, readonly=True, create=False)` and `env.begin(write=False)`.
Store a JSON map from each UTF-8 key to its original UTF-8 encoded LMDB value.
Do not read validator keys, wallet backups or genesis secrets. Run from the
workspace root with the node's environment:

The prior script is retired; recorded artifacts remain available for historical review.

The harness validates source/IR parity and typed-export agreement, then operates
only on a newly created temporary database. It reproduces the deployed permit
bypass with a disposable key, applies the two source changes through the patch
manager, compares every original state value, checks the exact added bookkeeping,
restarts storage, rejects the same old-key authority and exercises existing
allowance spending, direct revocation and direct transfers. The registry fixture
is only a contract-guard marker; this does not test wire authentication.

The completed local run is
`.artifacts/deployed-contract-audit/20261012-upgrade-rehearsal/rehearsal.json`:
**451 original state entries preserved except the four intended source/IR
artifacts**, with only expected patch bookkeeping added. Enrollment stayed off.
No live patch was applied. Rerun from the final release before approval.
