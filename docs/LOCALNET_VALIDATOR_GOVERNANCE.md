# Localnet Validator Governance

## Purpose

`scripts/localnet-validator-governance.py` is the focused live validation
program for validator, delegation, governance, and evidence behavior on a real
4-node localnet.

It exists to cover the operator-critical flows that are too coupled or too
stateful to trust to unit tests alone.

## Entry Point

Run it through the `xian-abci` project with local `xian-py` available:

```bash
uv run --project ../xian-abci --with ../xian-py \
  python3 ./scripts/localnet-validator-governance.py --bootstrap
```

Artifacts are written under:

```text
.artifacts/localnet-validator-governance/<run-id>/
```

## Live Coverage

The runner exercises these phases against a real localnet:

- generic governance proposal and voting
- manual validator membership, power update, remove, re-register, and re-add
- self-bonding, delegation, undelegation, and unbond claim
- `auto_top_n` selection and rebalance
- `hybrid` approval gating
- jail, unjail, and slash
- real CometBFT `DUPLICATE_VOTE` evidence submission
- `announce_leave`, early `leave` rejection, and rebalance while pending leave

The evidence phase confirms that:

- CometBFT accepts the evidence payload
- the target validator is jailed with `duplicate_vote`
- slash amount matches policy
- slashed funds reach the configured destination
- the live validator set replaces the removed validator

The leave phase confirms that:

- `announce_leave` succeeds
- immediate `leave` is rejected until the delay expires
- a pending-leave validator is excluded from the active set on rebalance
- validator status remains `leaving` until the actual exit path completes

## Important Regression

While extending the runner, we found a real contract bug in
`xian-configs/contracts/members.s.py`.

`pending_leave` stores either `False` or a `Datetime`. The rebalance path was
using a cross-type comparison instead of a truthiness check, which could raise a
runtime type error once a validator had announced leave.

That bug is now covered by:

- the live runner leave-announcement phase
- `xian-abci/tests/system/test_members.py::test_rebalance_excludes_validator_with_pending_leave`

## Current Constraint

`xian-configs/contracts/members.s.py` is now very close to the current contract
submission size ceiling. Future validator-policy additions should be treated as
size-sensitive work and checked before adding more code to the contract.

## Recommended Use

Use this runner when changing any of the following:

- validator selection policy
- delegation and unbonding behavior
- governance actions on `masternodes`
- evidence handling and slashing
- dashboard or query surfaces that depend on validator state

It is the highest-signal end-to-end check for this part of the stack.
