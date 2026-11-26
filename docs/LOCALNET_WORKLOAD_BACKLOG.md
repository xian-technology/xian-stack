# Localnet Workload Backlog

## Goal

Move localnet validation beyond simple transfers and toy contracts. The stack
should support repeatable, deterministic, consensus-focused workload scenarios
that can be extended over time as more complex contracts are added to the Xian
ecosystem.

## Current Coverage

`scripts/localnet-workload.py` supports `counter_basic`, `dex_mixed`,
`parallel_probe`, `transfer_fanout`, and `contract_heavy`. The DEX workload
exercises pair creation, liquidity operations, swaps, and expected failures
for expired deadlines, impossible minimum outputs, insufficient allowance, and
invalid pairs. It checks event counts and compares reserves, LP supply, and
token balances across validators.

The [live contract suite](../workloads/e2e/contract_suite/README.md) covers
additional execution and rollback cases through the integrated E2E harness.

## Proposed Dedicated Workloads

- Extend reusable scenario packs as more protocol coverage is needed.
- `governance_mixed`
  - proposal/vote/execute flows when suitable contracts exist
- `streaming_payments`
  - time-based flows, balance operations, and intentional settlement failures
- `adversarial_metering`
  - chi exhaustion
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
