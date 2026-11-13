"""Live switching of the zk runtime feature through governed state patches."""

from __future__ import annotations

from localnet_chain_checks import PROBE_SOURCE, agreement, deploy, query, require
from localnet_e2e_support import E2EError, short_hash
from localnet_runtime_safety_checks import failed_execution, successful
from xian_py.exception import SimulationError

ZK_FEATURE_KEY = "__runtime_features.zk"
ZK_DISABLED = "zk runtime feature is disabled"
ZK_PROBE_SOURCE = "@export\ndef check():\n    return zk.is_available()\n"
# The probe calls and zk deploys use a few thousand chi on the rev-14 VM.
ZK_TOGGLE_TX_CHI = 20_000
SHIELDED_PHASE = "15-shielded-note-token"


def shielded_token(phase_results) -> str | None:
    """The shielded note token deployed by this run, when its phase ran."""
    phase = next((item for item in phase_results if item.name == SHIELDED_PHASE), None)
    if phase is None or not phase.ok or phase.details.get("skipped"):
        return None
    return phase.details["token"]


async def committed_zk(session, nodes) -> dict[str, object]:
    return {node.moniker: await query(session, node, f"/get/{ZK_FEATURE_KEY}") for node in nodes}


async def switch_zk(runner, session, *, enabled: bool, label: str) -> dict:
    """Patch the zk feature with node-4 stopped through activation, then restart it."""
    armed = await runner.arm_state_patch(
        session,
        patch_id=f"zk-{label}-{short_hash(runner.run_id)}",
        summary=f"Switch the zk runtime feature {'on' if enabled else 'off'}",
        changes=[{"key": ZK_FEATURE_KEY, "value": enabled, "comment": f"zk {label}"}],
        label_prefix=f"zk-feature-{label}",
    )
    node4 = runner.nodes[4]
    print(f"[localnet-e2e] zk-feature-toggle stopping node-4 through zk {label}", flush=True)
    stop = await runner.stop_node_runtime(node4)
    await runner.wait_past_patch_activation(session, armed["activation_height"])
    start = await runner.start_node_runtime(
        session, node4, target_height=armed["activation_height"] + 1
    )
    committed = await committed_zk(session, runner.nodes)
    require(
        set(committed.values()) == {enabled},
        f"zk {label} patch did not reach every node: {committed}",
    )
    consensus = await agreement(session, runner.nodes, window=runner.args.app_hash_window)
    return {
        "bundle": armed["bundle"],
        "proposal_receipt": armed["proposal_receipt"],
        "node4_outage": {"stop": stop, "restart": start},
        "committed": committed,
        "app_hash": consensus,
    }


async def send(client, contract, function, kwargs=None):
    return await client.send_tx(
        contract, function, kwargs or {}, chi=ZK_TOGGLE_TX_CHI, wait_for_tx=True
    )


async def zk_runs(runner, session, probe, token, label) -> dict:
    # Each step opens its own client: deploys and patch votes move nonces.
    result = {"deploy": await deploy(runner, session, f"zk-toggle-{label}", ZK_PROBE_SOURCE)}
    async with runner.client(runner.founder_wallet, 0, session) as client:
        result["probe"] = successful(await send(client, probe, "check"), f"{label}: zk probe")
        if token is not None:
            result["shielded"] = successful(
                await send(client, token, "get_note_count"), f"{label}: shielded token call"
            )
    return result


async def zk_refused(runner, session, probe, plain, token) -> dict:
    """While zk is off every zk use fails with a receipt; other work is unaffected."""
    result = {}
    async with runner.client(runner.founder_wallet, 0, session) as client:
        result["probe"] = failed_execution(
            await send(client, probe, "check"), "zk probe while off", fragment=ZK_DISABLED
        )
        deploy_attempt = await client.deploy_contract(
            name=f"{probe}_off", source=ZK_PROBE_SOURCE, chi=ZK_TOGGLE_TX_CHI, wait_for_tx=True
        )
        result["deploy"] = failed_execution(
            deploy_attempt, "zk deploy while off", fragment=ZK_DISABLED
        )
        result["plain"] = successful(
            await send(client, plain, "bump", {"key": "zk-off"}), "plain call while zk is off"
        )
        if token is not None:
            result["shielded"] = failed_execution(
                await send(client, token, "get_note_count"),
                "shielded token call while off",
                fragment=ZK_DISABLED,
            )
        result["simulation"] = await refused_simulation(client, probe)
        require(
            await client.get_contract_source(f"{probe}_off") is None,
            "Refused zk deploy persisted",
        )
    result["app_hash"] = await agreement(session, runner.nodes, window=runner.args.app_hash_window)
    return result


async def refused_simulation(client, probe) -> str:
    try:
        simulated = await client.call(probe, "check", {})
    except SimulationError as exc:
        require(ZK_DISABLED in str(exc).lower(), f"zk simulation failed for another reason: {exc}")
        return str(exc)
    raise E2EError(f"zk simulation succeeded while off: {simulated!r}")


async def zk_toggle_phase(runner, session):
    initial = await committed_zk(session, runner.nodes)
    require(set(initial.values()) == {True}, f"localnet genesis must enable zk: {initial}")
    token = shielded_token(runner.phase_results)
    probe = await deploy(runner, session, "zk-toggle-probe", ZK_PROBE_SOURCE)
    plain = await deploy(runner, session, "zk-toggle-plain", PROBE_SOURCE)
    return {
        "probe": probe,
        "shielded_token": token,
        "initially_on": await zk_runs(runner, session, probe, token, "on"),
        "switched_off": await switch_zk(runner, session, enabled=False, label="off"),
        "refused_while_off": await zk_refused(runner, session, probe, plain, token),
        "switched_on": await switch_zk(runner, session, enabled=True, label="on-again"),
        "on_again": await zk_runs(runner, session, probe, token, "on-again"),
    }
