# Localnet Protocol Safety

## Purpose

`scripts/localnet-protocol-safety.py` is the focused live validation
program for validator, delegation, governance, and evidence behavior on a real
5-validator localnet seeded from the canonical `testnet` contract bundle.

It exists to cover the operator-critical flows that are too coupled or too
stateful to trust to unit tests alone.

## Entry Point

Run it through the `xian-abci` project with local `xian-py` available:

```bash
make localnet-protocol-safety
```

Artifacts are written under:

```text
.artifacts/localnet-protocol-safety/<run-id>/
```

## Live Coverage

The runner exercises these phases against a real localnet:

- generic governance proposal and voting
- governance state-patch approval, scheduling, and activation
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
- rebalance retains the eligible announcing validator in the active set
- the announcement deadline is unchanged by rebalance
- validator status remains `leaving` until the actual exit path completes

## Pending-Leave Regression

`pending_leave` stores either `False` or a `Datetime`. An already-active,
otherwise eligible validator remains selectable after announcing leave.
Rebalance does not complete its exit or bypass the notice period. The validator
must keep signing until a successful `leave()` removes it after the delay.

This behavior is covered by:

- the live runner leave-announcement phase
- `xian-configs/tests/test_system_contract_authorization.py::test_automatic_selection_keeps_announced_validator_until_leave`

Standby promotion is checked when the evidence phase actually removes a
validator. The live leave phase checks retention and early-exit denial; it
does not fast-forward the seven-day notice or claim to test delayed exit.

## Current Constraint

`xian-configs/contracts/validators.s.py` is now very close to the current contract
submission size ceiling. Future validator-policy additions should be treated as
size-sensitive work and checked before adding more code to the contract.

## Recommended Use

Use this runner when changing any of the following:

- the canonical `testnet` genesis bundle or its validator policy surface
- validator selection policy
- delegation and unbonding behavior
- governance actions on `validators`
- governance state-patch lifecycle
- evidence handling and slashing
- dashboard or query surfaces that depend on validator state

It is the highest-signal end-to-end check for this part of the stack.
