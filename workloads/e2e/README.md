# E2E Workloads

This folder contains small, deterministic helper contracts used by the
multi-phase localnet end-to-end runner.

- `conflict_guard.py`: deterministic conflict and failure surface
- `orchestration_factory.py`: deploys multiple child contracts from a contract
  using templated artifact bundles rendered by `localnet-e2e.py`
- `orchestration_child.py`: canonical child contract source for the
  orchestration deployment flow
- `orchestration_router.py`: dynamic contract and function dispatch helper
- `orchestration_mid.py`: mid-hop contract for caller/signer chain checks
- `orchestration_root.py`: root-hop contract for nested ctx semantics
- `patch_target.py`: simple state-patch target contract

```mermaid
flowchart LR
  E2E["localnet-e2e.py"] --> Factory["orchestration_factory"]
  Factory --> Child["orchestration_child"]
  Factory --> Router["orchestration_router"]
  Router --> Mid["orchestration_mid"]
  Mid --> Root["orchestration_root"]
  E2E --> Conflict["conflict_guard"]
  E2E --> Patch["patch_target"]
```
