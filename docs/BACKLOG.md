# Backlog

This is the stable backlog entrypoint for `xian-stack`.

Primary deeper notes:

- `LOCALNET_WORKLOAD_BACKLOG.md`
- `RUNTIME_RELEASE_BACKLOG.md`
- [BDS throughput investigation](BDS_THROUGHPUT_INVESTIGATION.md): measured
  evidence, source paths and the post-release saturation/recovery campaign

Current themes:

- runtime/release hardening
- workload expansion
- backend/operator ergonomics
- keeping stack docs aligned with the current 5-validator validation flows in
  `xian-docs-web/node/localnet-e2e.md`

## Transaction Throughput

- [Transaction throughput optimization plan](../../xian-meta/docs/TRANSACTION_THROUGHPUT_OPTIMIZATION_PLAN.md): proposed staged work
  on measurement, bounded immutable admission reuse, proposal scans, live
  admission and receipt costs. Preserve current execution semantics and
  remeasure before pursuing parallel contract execution.
