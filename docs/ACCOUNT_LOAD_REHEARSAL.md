# Sustained account load and restart rehearsal

`scripts/account-load-rehearsal.py` creates five disposable native validators
from current sibling sources. It keeps enrollment closed, including in the test
genesis. Four hybrid identities are pre-registered as fixtures; a fifth identity
uses its original Ed25519 key. The harness never targets an existing RPC, writes
the final network genesis, or changes shipping configuration.

## Running the drill

Sync the stack Python environment, including the local node, native VM and
Python SDK hybrid extra. Use the release-pinned CometBFT binary for a release
gate. Choose unused loopback ports and a new output directory:

```bash
uv run python scripts/account-load-rehearsal.py \
  --cometbft /path/to/cometbft --base-port 40650 \
  --baseline-seconds 60 --attack-seconds 180 --recovery-seconds 120 \
  --invalid-rps 80 --output .artifacts/account-load/new-run
```

The baseline runs ordinary and hybrid transfers. The next phase mixes transfers
with invalid Ed25519/ML-DSA proofs, valid proofs with invalid nonces, wrong-chain
transactions, original-key bypass attempts, closed-enrollment attempts, deeply
nested JSON, oversized JSON and malformed hex. Eight bounded clients distribute
the configured invalid request rate across live validators. Transport failures
and duplicate-cache hits are reported separately from validation rejections.

During that phase the runner kills/restarts one validator, then kills/restarts
all five while transfers are in flight. A sender retries exactly the same bytes
until a successful committed receipt resolves its transaction. It then reopens
an encrypted master backup, derives recovery authority, saves and reopens one
fresh master backup, commits a replacement key epoch, checks obsolete
active-key rejection on all validators and resumes spending under invalid
traffic. Recovery submission itself occurs between the attack phases. Finally,
all validators restart once more and every account must spend again.

## Pass conditions and artifacts

Each phase requires progress from every account. At each checkpoint all five
validators must finish catching up, agree on a historical application hash,
retain the closed policy and expected authority records, and match an independent
balance ledger derived from unique successful receipts. The ledger rejects nonce
gaps and duplicates, allowing only the explicitly recorded recovery nonce.
Transfers conserve the initial 50,000 XIAN fixture allocation. The development
founder funds these fixtures; they are not additional minted funds.

`report.json` contains phase counts, latency percentiles, RPC failures, account
epochs, convergence checkpoints, source hashes and CometBFT binary/module
identity. `committed-transfers.json` contains receipt hashes and accounting inputs.
Periodic resource samples record process CPU/RSS, log size and block heights.
The process runner cleans up only its own validators, even after a failure.

Output directories and backup files are private to the local user. Backup
passwords are ephemeral and not retained; these are test credentials, not usable
custody backups. Do not publish the entire artifact directory.

## Scope of the evidence

This is a bounded application/RPC abuse and crash-recovery test on one host,
using `free_metered` fees and one in-flight transfer per sender. Its transfer
rate is not a maximum-throughput measurement. It does not establish distributed
network DoS capacity, production resource limits, paid-fee behavior under load,
or release readiness. Traffic stops briefly for convergence checkpoints.

A passing report does not enable enrollment. Production-like deployment testing
with the exact release binary, paid-fee accounting, ingress/resource limits,
independent security review and wallet/device rollout checks remain separate
gates. Final migration allocation and genesis remain deferred.

## Local result: 2026-10-12

Run `.artifacts/account-load/20261012-sustained-1/report.json` passed with the
parameters above. The local Python interpreter was the sibling CLI development
environment, containing the editable node, VM and SDK dependencies. The installed
CometBFT binary reported CLI version `0.39.0`, with Go module identity `v0.39.1`.
This is **not** the configured release pin `0.39.3`; repeat on that exact release
before treating this as release evidence.

| Phase | Requested duration | Committed transfers | Invalid attempts | Commit p95 |
| --- | ---: | ---: | ---: | ---: |
| Baseline | 60 s | 290 | 0 | 1.69 s |
| Mixed traffic and crashes | 180 s | 811 | 13,724 | 1.99 s |
| Recovered spending under attack | 120 s | 653 | 9,338 | 1.08 s |
| Spending after final restart | 10 s | 55 | 0 | 1.25 s |

All 1,809 transfers matched the exact receipt ledger, with no duplicate payment
and no lost fixture funds. Six checkpoints verified all five validators. The
final fixture balance sum was 50,000 XIAN; the recipient held exactly 1,809 XIAN.
All five accounts made progress in every phase. The separate successful recovery
consumed one nonce and moved no funds; obsolete active credentials were rejected
by every validator. Enrollment was closed throughout.

Of 23,062 invalid submissions, 22,586 returned validation rejection and 476 hit
transport/catch-up unavailability during the crash phase. No invalid request was
accepted and no invalid-cache hit masked validation. Both attack phases exercised
every corpus category. Node logs confirmed distinct authorization, closed-policy,
nonce, JSON-depth, wire-size and malformed-hex rejection paths. Invalid-response
p95 was 22.39 ms during crashes and 22.51 ms after recovery. Achieved invalid
request rates were 76.24/s and 77.82/s against the 80/s target. The longest valid
transfer took 8.61 s during the crash phase and eventually committed exactly once.

The sampled maximum combined RSS of the validator/ABCI processes was 787.5 MiB;
the largest sampled aggregate log size was 12.14 MB. Samples are taken every ten
seconds and do not establish peak resource requirements or leak freedom. This
was a shared workstation, including brief repository validation work during the
run, and no maximum-throughput inference should be drawn from these timings.
All disposable validator processes were stopped after completion.

Related validation passed: 255 stack tests plus 74 subtests, stack preflight,
focused Ruff checks, public documentation build and workspace conventions. The
short smoke run exposed a harness retry gap for CometBFT's catch-up RPC response;
the harness now treats that exact response as temporary unavailability while
preserving exact-byte retry and receipt confirmation. This run required no new
production authorization change.

## Single-master result: 2026-10-12

Run `.artifacts/account-load/20261012-single-master-1/report.json` passed after
the SDK custody update. Each fixture now saves one master file per epoch; cold
recovery and subsequent spending derive their respective credentials from that
file. The replacement master is saved and reopened before transition signing.

With phases of 30/90/60/10 seconds, 889 committed transfers matched the exact
receipt ledger. All five accounts made progress after recovery and restarts;
six checkpoints verified all five validators and conserved the 50,000 XIAN
fixture allocation. Of 11,378 invalid attempts, 10,922 were rejected and 456
encountered transport unavailability during crashes. None were accepted.
Obsolete active credentials were rejected after recovery. Enrollment stayed
closed; no final genesis was created.

This run used the same local CometBFT CLI `0.39.0` / Go module `v0.39.1`, rather
than release pin `0.39.3`, and `free_metered` fees. The release, distributed-load,
paid-fee and independent-review limitations above still apply.
