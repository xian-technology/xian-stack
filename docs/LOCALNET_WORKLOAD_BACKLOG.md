# Localnet Workload Backlog

## Goal

Move localnet validation beyond simple transfers and toy contracts. The stack
should support repeatable, deterministic, consensus-focused workload scenarios
that can be extended over time as more complex contracts are added to the Xian
ecosystem.

## Current Gap

`scripts/localnet-burst-test.py` proves that a multi-node network stays alive
under a basic mixed load and that nodes agree on `app_hash` across sampled
heights. It does not yet provide:

- reusable scenario packs
- realistic contract-heavy workloads
- deterministic failure scenarios
- contract-state cross-checks beyond `app_hash`
- a clean extension point for future protocol hardening

## Proposed Direction

Build a scenario runner around a deterministic workload plan.

Each scenario should:

- deploy a frozen, vendored contract pack from the repo
- derive wallets, nonces, and tx order from a fixed seed
- record an explicit tx plan before execution
- run successful and intentionally failing transactions
- verify consensus with `app_hash` checks across multiple heights
- verify selected contract state across multiple nodes at the same height
- verify expected failure classes and event counts

## Initial Scenario Packs

- `counter_basic`
  - simple transfer and state mutation sanity check
- `dex_mixed`
  - pair creation
  - liquidity add/remove
  - exact-input swaps
  - fee-on-transfer compatible swaps
  - intentional failures: expired deadline, insufficient allowance,
    impossible `amountOutMin`, invalid pair
- `governance_mixed`
  - proposal/vote/execute flows when suitable contracts exist
- `streaming_payments`
  - time-based flows, balance operations, and intentional settlement failures
- `adversarial_metering`
  - stamp exhaustion
  - oversized return values
  - contracts designed to trigger conflict-heavy or high-iteration paths

## Contract Pack Policy

- Do not fetch contracts from GitHub during tests.
- Vendor canonical workload contracts into the repo or into
  `xian-configs` test fixtures.
- Version the contract packs so workload changes are explicit and reviewable.
- Keep the workload runner able to add more contract packs over time without
  rewriting the harness.

## Verification Requirements

For each scenario:

- block heights converge across all nodes
- `app_hash` matches across a sampled height window
- selected contract variables match across sampled nodes
- expected failed transactions fail consistently
- expected events appear consistently
- summary output is machine-readable for CI use

## Rollout Order

1. Refactor `localnet-burst-test.py` into a scenario runner.
2. Add a vendored DEX scenario.
3. Add deterministic failure cases to the DEX scenario.
4. Add state cross-checks in addition to `app_hash`.
5. Add more contract packs over time as the ecosystem grows.
