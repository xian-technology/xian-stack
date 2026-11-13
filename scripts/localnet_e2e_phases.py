from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from localnet_e2e_support import E2EError

PhaseCallable = Callable[[], Coroutine[Any, Any, dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class PhaseSpec:
    name: str
    method_name: str
    uses_session: bool = True


PHASE_SPECS: tuple[PhaseSpec, ...] = (
    PhaseSpec("00-bootstrap", "bootstrap"),
    PhaseSpec("01-health", "health_phase"),
    PhaseSpec("02-abci-consistency", "abci_consistency_phase"),
    PhaseSpec("02-ed25519-genesis", "signature_phase"),
    PhaseSpec("02-xian-py-smoke", "xian_py_smoke"),
    PhaseSpec("03-contract-orchestration", "contract_orchestration_phase"),
    PhaseSpec("03-contract-functionality", "contract_functionality_phase"),
    PhaseSpec("03-atomic-rollback", "atomic_rollback_phase"),
    PhaseSpec("03-x402-exact", "x402_exact_phase"),
    PhaseSpec("03-intentkit-x402", "intentkit_x402_phase"),
    PhaseSpec("04-periodic-load", "periodic_load"),
    PhaseSpec("05-burst-load", "burst_phase", uses_session=False),
    PhaseSpec("06-conflict-invalid", "conflict_phase"),
    PhaseSpec("07-dex-mixed", "dex_phase", uses_session=False),
    PhaseSpec("07-dex-automation-recovery", "dex_automation_recovery_phase"),
    PhaseSpec("08-throughput-mix", "throughput_mix_phase", uses_session=False),
    PhaseSpec("08-simulator-load", "simulator_phase"),
    PhaseSpec("09-bds-catchup", "bds_catchup_phase"),
    PhaseSpec("10-retrieval-surfaces", "retrieval_phase"),
    PhaseSpec("11-determinism", "determinism_phase"),
    PhaseSpec("11-storage-scan-determinism", "storage_scan_phase"),
    PhaseSpec("12-validator-governance", "validator_governance_phase"),
    PhaseSpec("13-state-patch", "state_patch_phase"),
    PhaseSpec("14-logging", "logging_phase"),
    PhaseSpec("15-shielded-note-token", "shielded_phase"),
    PhaseSpec("16-block-ordering", "block_ordering_phase"),
    PhaseSpec("17-chaos-convergence", "chaos_convergence_phase"),
    PhaseSpec("18-soak-abuse", "soak_abuse_phase"),
    PhaseSpec("19-crash-recovery", "crash_recovery_phase"),
    PhaseSpec("20-quorum-recovery", "quorum_recovery_phase"),
    PhaseSpec("21-fresh-node-sync", "fresh_node_phase"),
    PhaseSpec("23-nonce-recovery", "nonce_recovery_phase"),
    PhaseSpec("24-accounting-invariants", "accounting_invariants_phase"),
    PhaseSpec("25-block-limits", "block_limits_phase"),
    PhaseSpec("26-replay-determinism", "replay_corpus_phase"),
)


def suite_phases(suite: str = "full") -> tuple[PhaseSpec, ...]:
    if suite == "full":
        return PHASE_SPECS
    if suite == "intentkit-x402":
        required = {
            "00-bootstrap",
            "01-health",
            "02-abci-consistency",
            "03-x402-exact",
            "03-intentkit-x402",
        }
        return tuple(phase for phase in PHASE_SPECS if phase.name in required)
    raise ValueError(f"unknown E2E suite: {suite}")


def phase_names(suite: str = "full") -> list[str]:
    return [phase.name for phase in suite_phases(suite)]


def validate_suite(suite: str, *, intentkit_enabled: bool, require_shielded: bool = False) -> None:
    if suite == "intentkit-x402" and not intentkit_enabled:
        raise E2EError("intentkit-x402 suite requires --intentkit-x402")
    if require_shielded and suite != "full":
        raise E2EError("--require-shielded requires the full suite")


def validate_shielded_runtime(*, required: bool, available: bool) -> None:
    if required and not available:
        raise E2EError(
            "native ZK support is required but unavailable; run make localnet-e2e "
            "using the locked xian-stack Python environment"
        )


def required_phase(name: str, *, suite: str, require_shielded: bool) -> bool:
    return suite != "full" or (require_shielded and name == "15-shielded-note-token")


def validate_resumed_phases(
    results: list[Any], *, suite: str, require_shielded: bool, start_phase: str
) -> None:
    names = phase_names(suite)
    prior_results = {result.name: result for result in results}
    for name in names[: names.index(start_phase)]:
        if not required_phase(name, suite=suite, require_shielded=require_shielded):
            continue
        result = prior_results.get(name)
        if result is None or not result.ok or result.details.get("skipped"):
            raise E2EError(f"resume requires successful, non-skipped required phase {name}")


def bind_phase(runner: Any, phase: PhaseSpec, session: Any) -> PhaseCallable:
    method = getattr(runner, phase.method_name)
    if phase.uses_session:
        return lambda: method(session)
    return method


def bind_phase_sequence(
    runner: Any,
    session: Any,
    *,
    suite: str = "full",
) -> list[tuple[str, PhaseCallable]]:
    return [(phase.name, bind_phase(runner, phase, session)) for phase in suite_phases(suite)]
