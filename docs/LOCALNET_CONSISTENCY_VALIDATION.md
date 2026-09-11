# Localnet consistency validation

Historical run: 2026-09-11. The runtime defect recorded below was subsequently
fixed and verified; see [the follow-up validation](FOREIGN_STORAGE_VALIDATION.md)
for current node state and results.

Run date: 2026-09-11. A five-validator Docker localnet was built from the current
sibling working trees and exercised with the integrated E2E suite, protocol
safety suite, and live SDK/product regressions. The node suites passed. Two
SDK/frontend defects were fixed and retested. One native-VM defect remains open
and blocks checker-based NFT discovery, profiles, and activity.

These are local working-tree results, not a published release or public-network
acceptance. The previous consistency audit's five fixes were included in testing.

## Running environment

The final network is left running with five validators and a PostgreSQL BDS
indexer on node 0:

- RPC: `http://127.0.0.1:27657`; chain ID: `xian-localnet-1`.
- Other RPC ports: 27757, 27857, 27957, 28057, all on loopback.
- Runtime: `xian_vm_v1`, metered paid transactions, Linux aarch64.
- Image: `sha256:ac0506709bb0e03b2e9051c2ba0260cb5dcc5c30eec50e7f572c9b9761213fdb`.
- Docker allocation: 8 CPUs, 8 GiB. All five validators agreed at a shared
  height after testing; none was catching up.
- Canonical `testnet` genesis contracts were used on this isolated local chain.
  No public RPC endpoint or public chain ID was added to code defaults.

From `xian-stack`:

```bash
make localnet-status
make localnet-node-report
make localnet-down       # stop the generated localnet
make localnet-up         # restart its existing state
```

The earlier `.localnet` and generated compose file were preserved under
`.artifacts/localnet-before-consistency-20260911T005849Z/`; its PostgreSQL volume
was retained. Test runners created fresh disposable networks between suites.
Keys in `.localnet/network.json` belong only to this local test network.

## Results

| Surface | Result and scope |
| --- | --- |
| Integrated node E2E | Passed 31 executed phases. One optional IntentKit phase skipped, excluded from the pass count. |
| Native replay | Replayed and compared 818 blocks from scratch, including app hashes, transaction outcomes, chi, and events. |
| Loads and failure recovery | Passed periodic/burst load (260 burst transactions), conflict/invalid transactions, DEX workload, throughput mix, simulator, BDS catch-up, atomic rollback, parallel/mixed execution, chaos, crash, quorum recovery, fresh-node sync, nonce recovery, accounting invariants, block limits, and a 90-second soak. |
| Protocol safety | Passed governance proposals/voting, state-patch activation, manual membership, self-bond/delegation/undelegation/unbond claim, automatic and hybrid selection, jail/unjail/slash, real duplicate-vote evidence, early-leave rejection and standby promotion. |
| Shielded and x402 | Integrated suite passed real shielded proof/signature, registry/relayer/authentication/error cases and x402 exact payments. |
| Python/JS wire regressions | All five shared fixtures finalized on-chain from each SDK; JS native nested BigInt also passed. VM assertions verified nested integer, boolean, decimal, and bytes types after storage. |
| NFT bundle and transactions | Modified bundles rejected without advancing signer nonce. Valid bootstrap and repeat bootstrap passed. Exact submitted source matched the bundle; stored source matched compiler normalization. Mint, precise-price list/buy, transfer, and burn passed. |
| Stable protocol | Valid bootstrap/repeat and controller checks passed. PSM mint/redeem with fees, savings deposit/withdraw, and vault open/repay/close passed. Vault stability fee set to zero for deterministic repayment in this test. |
| JavaScript DEX adapter | Live approval and swap passed against the canonical DEX bundle. |
| MCP | Actual stdio transport: three exact-message sign/verify round trips passed; trimmed-message verification failed as expected. Live BDS/node read passed. |
| Bridge | Optional PostgreSQL migration and headless-browser template tests: 10 passed. Temporary test database/container removed. |
| Compiler WASM | Optional compiler build and both WASM tests passed. |
| NFT browser | Collection grid, owner count, token content/hash display and UTC timestamp passed in Chromium using Berlin and New York timezones. Profile/activity remain blocked by the checker runtime defect below. No JavaScript page exceptions. |

Replay ended at height 818 with app hash
`39609457f6209705dd09da5d4fd0f07e19bb2235b3dd89f7727e34f9d6b3fb8f`.
The final running network is a separate fresh chain containing DEX, NFT, stable,
and regression-probe contracts; its app hash is therefore different.

## Defects fixed during the live checks

1. **Indexed JSON decoding.** BDS returned `data` and `data_indexed` as JSON
   strings. The JS client treated them as absent, and NFT readers ignored the
   indexed identifiers. The client now decodes JSON-text columns (also indexed
   transaction payloads), preserving large integers and own prototype-named
   properties. NFT event reads use the SDK and combine indexed/non-indexed data.
   A null recent-events response is correctly marked unavailable.
2. **UTC timestamps.** Offset-free chain timestamps were interpreted in the
   browser's timezone, showing a fresh NFT as two hours old in Berlin. Shared
   `maybeDate` interprets these timestamps as UTC and preserves explicit offsets.

Post-change validation: SDK type/build checks, 108 tests and 6 release tests;
NFT type/build checks and 41 tests; browser wallet validation (89 tests plus 6
release tests); mobile type checks and 79 tests; governance UI validation and 42
tests; public documentation build. Date regressions also ran in Berlin and New
York process timezones. The normal SDK suite skips two optional WASM tests;
those two passed separately. Live BDS event and transaction-payload decoding
was verified with the rebuilt SDK.

## Runtime defect found: dynamic ForeignHash

The standard NFT checker deploys but `con_xsc005.is_XSC005` fails for the valid
`con_cons_nft` collection:

```text
VmRuntimeExecutionError: unsupported host syscall 'storage.foreign_hash.new'
```

This was reproduced through both readonly simulation and a finalized chain
transaction (`2D063B770E48C223728DDFB625F8F9CD3E5E452029B29CC8A5CA4D22224CE321`).
The failure is in the native runtime, not BDS decoding or the collection's mint
and transfer functions. It also reproduces with the standard checker contract
name after the initially custom-named checker was supplemented.

The checker creates a `ForeignHash` inside its exported function. The compiler
accepts and emits this call; the VM handles module-level storage declarations
but forwards the dynamic constructor to a host without that syscall. Supporting
it requires complete reference/read behavior, metering and read-only enforcement,
not just accepting the constructor. No runtime semantics were changed in this
live-test follow-up. The subsequent fix and passing acceptance checks are recorded in
[the follow-up validation](FOREIGN_STORAGE_VALIDATION.md).

Reproduce read-only from `xian-stack` while the test chain is running:

```bash
node .artifacts/consistency-live/nft-checker-repro.mjs
```

This command exited 1 on the original network. Browser profile/activity failures
are recorded as blocked; they are not counted as successful checks.

## Evidence and limits

Local generated evidence is intentionally ignored by Git:

- `.artifacts/localnet-e2e/20260911T005850Z/summary.json` and per-phase reports,
  replay corpus, and native-replay output.
- `.artifacts/localnet-protocol-safety/20260911T013706Z/summary.json` and phase reports.
- `.artifacts/consistency-live/`: live regression scripts, transaction hashes,
  bootstrap logs, MCP/bridge results, browser screenshots/results, checker
  reproductions, final consensus check, and source provenance (HEAD plus changed
  file hashes). These local scripts depend on the retained disposable test state;
  `checks.py` includes fixed token/vault IDs and is not a general repeatable CI job.

The integrated runners are repeatable via `make localnet-e2e` and
`make localnet-protocol-safety`; they initialize disposable networks. Preserve
any state you need before running them again. The final localnet was initialized
with BDS enabled, node index 0, port offset 1000, and five nodes.

Limits: one native architecture, short soak rather than a multi-day endurance
run, no full seven-day delayed-leave completion, no real light-client-attack
evidence, no actual cross-chain custody transfer, no mobile-device session, and
no injected-wallet approval UI transaction flow. IntentKit remained excluded.
The NFT checker defect means the full marketplace is not yet a passing native-VM
end-to-end flow. Earlier passing unit tests and replay do not establish otherwise.
