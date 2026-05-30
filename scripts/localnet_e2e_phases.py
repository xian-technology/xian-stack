from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any


PhaseCallable = Callable[[], Coroutine[Any, Any, dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class PhaseSpec:
    name: str
    method_name: str
    uses_session: bool = True


PHASE_SPECS: tuple[PhaseSpec, ...] = (
    PhaseSpec("00-bootstrap", "bootstrap"),
    PhaseSpec("01-health", "health_phase"),
    PhaseSpec("02-xian-py-smoke", "xian_py_smoke"),
    PhaseSpec("03-contract-orchestration", "contract_orchestration_phase"),
    PhaseSpec("03-atomic-rollback", "atomic_rollback_phase"),
    PhaseSpec("03-x402-exact", "x402_exact_phase"),
    PhaseSpec("03-intentkit-x402", "intentkit_x402_phase"),
    PhaseSpec("04-periodic-load", "periodic_load"),
    PhaseSpec("05-burst-load", "burst_phase", uses_session=False),
    PhaseSpec("06-conflict-invalid", "conflict_phase"),
    PhaseSpec("07-dex-mixed", "dex_phase", uses_session=False),
    PhaseSpec("08-throughput-mix", "throughput_mix_phase", uses_session=False),
    PhaseSpec("08-simulator-load", "simulator_phase"),
    PhaseSpec("09-bds-catchup", "bds_catchup_phase"),
    PhaseSpec("10-retrieval-surfaces", "retrieval_phase"),
    PhaseSpec("11-determinism", "determinism_phase"),
    PhaseSpec("12-validator-governance", "validator_governance_phase"),
    PhaseSpec("13-state-patch", "state_patch_phase"),
    PhaseSpec("14-logging", "logging_phase"),
    PhaseSpec("15-shielded-note-token", "shielded_phase"),
    PhaseSpec("16-parallel-execution", "parallel_execution_phase"),
    PhaseSpec("17-chaos-convergence", "chaos_convergence_phase"),
    PhaseSpec("18-soak-abuse", "soak_abuse_phase"),
)


def phase_names() -> list[str]:
    return [phase.name for phase in PHASE_SPECS]


def bind_phase(runner: Any, phase: PhaseSpec, session: Any) -> PhaseCallable:
    method = getattr(runner, phase.method_name)
    if phase.uses_session:
        return lambda: method(session)
    return method


def bind_phase_sequence(
    runner: Any,
    session: Any,
) -> list[tuple[str, PhaseCallable]]:
    return [(phase.name, bind_phase(runner, phase, session)) for phase in PHASE_SPECS]
