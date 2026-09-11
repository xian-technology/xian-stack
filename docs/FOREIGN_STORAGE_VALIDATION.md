# Dynamic foreign storage validation

Validated locally on 2026-09-11. The native VM defect that prevented
`con_xsc005.is_XSC005` from running is fixed. The shipped checker, collection
discovery, owner profile, and activity feed execute successfully against the
rebuilt five-validator localnet. Contract sources and bundle hashes were not
changed to work around the runtime.

## Runtime behavior

`ForeignHash` and `ForeignVariable` can be constructed inside functions using
`foreign_contract` and `foreign_name` keyword arguments. A native reference
contains its target and kind, with no writable binding or value cache.

Hash indexing, prefix `.all()` scans and variable `.get()` calls use the host
storage API and existing operation/byte charges. Each read sees current
transaction state; aliases keep their target. Targets must be single ASCII
identifiers, preventing composed keys from escaping the selected namespace.
Missing values return `None`. Writes and mutating methods are rejected, and
references cannot be serialized across the Python host boundary.

Implementation lives in `xian-contracting/packages/xian-vm-core/src/foreign_storage.rs`,
with interpreter dispatch and a dedicated VM value type. The compiler IR and
static storage paths are unchanged.

## Validation

| Check | Result |
| --- | --- |
| Contracting default and optional-native Python suites, including security/integration | 891 passed, plus 20 subtests |
| New dynamic foreign-storage integration tests | 24 passed, included above: Python/native read parity, missing values, multidimensional keys, scans, aliases, live cross-contract updates, forbidden writes, namespace checks, metering and exhausted budgets |
| VM Rust unit and parity tests | 63 passed (43 unit, 20 parity fixtures) |
| ABCI full suite with rebuilt native extension | 693 passed, 1 unrelated skip, plus 56 subtests |
| NFT Python suite with explicit native extension | 12 passed, including 4 shipped-checker tests for valid and invalid collections/metadata |
| NFT frontend | Type checking, 41 tests and production build passed |
| Lint, formatting and workflow validation | Python checks and NFT actionlint passed; changed-file whitespace checks passed |
| Public documentation | Production build passed |
| Previous E2E corpus on corrected image | 818 blocks replayed with identical per-block app hashes, transaction outcomes, chi and events |
| Fresh corpus containing the fix | 86 blocks replayed successfully, including successful checker/variable reads and rejected foreign writes |
| Live SDK/product regressions | Python/JS wire fixtures, NFT bundle/bootstrap/lifecycle and stable-protocol flows passed on the fresh chain |
| Live checker | Valid collection returns true; invalid interface returns false; finalized checker calls succeed; all five validators agree |
| Live browser | Discovery from empty storage, collection/token views, owner profile and activity pass in Chromium with Berlin and New York timezones |

The NFT CI workflow includes a native checker gate that explicitly builds the
sibling VM extension; a skipped optional module cannot stand in for that gate.
The regular Python suite can still run without the optional native extension.

The original integrated E2E suite was not rerun from start in this follow-up.
Its complete 818-block corpus was replayed under the corrected image, and a
fresh corpus was captured for the new behavior. These checks ran on Linux
ARM64; they do not substitute for release-time validation on other architectures.

## Live evidence

Image:
`sha256:afd9dccabb9724ea00fb5c28996940b6e5044c327a8fd2e97e94dbe6e5ef0503`.

Representative finalized transactions:

- Valid checker: `701339AA0EEF758C9DBE22FB488622E97A2C66BEECDD2EF2950C08EFA7235727`, 135 chi.
- Invalid interface (successful call returning false): `F0F6EE911001DE44FE06CCAAFC536FBCA7E384B852A7D6FF6C0C759786BE674B`, 125 chi.
- Dynamic variable read: `44D87AB29D90411E3F201741762518F1C4F03AE1E3B0B356A0209BD0DEA5C5EF`, 21 chi.
- Rejected foreign write: `E83A3C4382D8FE8AE1C8D24A6C007DD34DFF63BEE1E98B0184216A71C807836C`, 15 chi. Collection standard remained `XSC-0005`.

The old corpus retained app hash
`39609457f6209705dd09da5d4fd0f07e19bb2235b3dd89f7727e34f9d6b3fb8f` at height 818.
The fresh corpus produced
`70d3e4ff5cd9ff0dcdc47a70769f1cff46f1a352ddfd1b0936e324acf2b231c0` at height 86.

Local scripts, transaction receipts, browser screenshots, both replay outputs,
source hashes and logs are preserved under `.artifacts/foreign-storage-fix/`.
They are ignored generated artifacts. The scripts use the retained disposable
chain and fixed fixture IDs; they are not all safe to rerun against populated
state. The permanent runtime and NFT tests are repository source changes.

## Running localnet

The corrected five-validator network with PostgreSQL indexing remains running
at `http://127.0.0.1:27657`, chain ID `xian-localnet-1`. The prior network was
preserved under `.artifacts/foreign-storage-fix/previous-localnet/`, including
its compose file, with PostgreSQL volume
`xian-localnet-postgres-866418edda182e30` retained separately.

A fresh chain was used because the previous history contains checker calls that
failed with the old VM. Their outcome changes under the fix. Runtime semantics
are consensus-sensitive: all validators must run the same accepted version;
this local validation is not a public deployment or release announcement.

From `xian-stack`, use `make localnet-node-report` to check health,
`make localnet-down` to stop, and `make localnet-up` to restart existing state.
