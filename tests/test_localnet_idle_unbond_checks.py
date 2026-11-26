"""Offline behavioral tests for the maintained staking-clock live preflight."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).absolute().parents[1] / "scripts"))
import localnet_idle_unbond_checks as checks


def initial_fixture():
    nodes = [{"address": letter * 40, "account": letter.lower() * 64} for letter in "ABC"]
    low = nodes[1]
    native = {"validators": [{"address": node["address"], "voting_power": "10"} for node in nodes]}
    consensus = {
        "round_state": {
            "height/round/step": "1/0/4",
            "proposer": {"address": nodes[0]["address"]},
            "height_vote_set": [
                {
                    "round": 0,
                    "prevotes": [
                        "nil-Vote",
                        f"Vote{{1:{low['address'][:12]} 1/00/1(Prevote) "
                        "000000000000 ABCDEFABCDEF 000000000000 @ 2026-01-01T00:00:00Z}",
                        "nil-Vote",
                    ],
                }
            ],
        }
    }
    powers = {node["account"]: 10 for node in nodes}
    return consensus, native, powers, nodes, low


def test_live_precondition_requires_actual_nil_prevote_from_a_nonproposer():
    consensus, native, powers, nodes, low = initial_fixture()
    consensus["round_state"]["height_vote_set"][0]["prevotes"][1] = "nil-Vote"
    with pytest.raises(Exception, match="NIL prevote"):
        checks.validate_initial_state(consensus, native, powers, nodes, low)


def test_live_precondition_rejects_initchain_power_mismatch():
    consensus, native, powers, nodes, low = initial_fixture()
    native["validators"][0]["voting_power"] = "20"
    with pytest.raises(Exception, match="power"):
        checks.validate_initial_state(consensus, native, powers, nodes, low)


def test_live_precondition_rejects_starting_the_proposer_alone():
    consensus, native, powers, nodes, low = initial_fixture()
    consensus["round_state"]["proposer"]["address"] = low["address"]
    with pytest.raises(Exception, match="proposer"):
        checks.validate_initial_state(consensus, native, powers, nodes, low)


def test_old_genesis_scenario_fails_if_the_actual_first_block_is_fresh():
    headers = [{"height": str(index), "time": "2026-01-01T00:00:00Z"} for index in [1, 2, 3]]
    with pytest.raises(Exception, match="genesis"):
        checks.validate_stale_headers(headers, 0, 1767225600000000000, 1767225600000000000)


def test_maturity_anchor_must_come_from_successor_not_stale_creation_time():
    record = {
        "created_block": 1,
        "unbonding_period_days": 14,
        "unlock_at": {"__time__": [1970, 1, 15, 0, 0, 0]},
        "claimed": False,
    }
    header = {"height": "2", "time": "2026-01-01T00:00:00.000000789Z"}
    with pytest.raises(Exception, match="successor"):
        checks.validate_anchor(record, 1, header, 14)


def test_failed_early_claim_keeps_exact_stake_principal_in_custody():
    with pytest.raises(Exception, match="custody"):
        checks.validate_locked_finances(
            {"self_bond": "0", "custody": "0"},
            {"self_bond": "0", "custody": "0"},
            {"amount": "1", "claimed": False},
            1,
        )


def headers_fixture():
    return [
        {"height": "1", "time": "2026-01-01T00:00:00Z"},
        {"height": "2", "time": "2026-04-11T00:00:00.000000789Z"},
        {"height": "3", "time": "2026-04-11T00:00:01Z"},
    ]


def locked_record(*, owner="a" * 64, created=1, anchor="2026-04-11T00:00:00Z"):
    from datetime import datetime, timedelta

    unlock = datetime.fromisoformat(anchor.replace("Z", "+00:00")) + timedelta(days=14)
    return {
        "id": 1,
        "owner": owner,
        "validator": owner,
        "amount": "1",
        "kind": "self_bond",
        "created_block": created,
        "created_at": {"__time__": [2026, 1, 1, 0, 0, 0]},
        "unlock_at": {
            "__time__": [
                unlock.year,
                unlock.month,
                unlock.day,
                unlock.hour,
                unlock.minute,
                unlock.second,
                unlock.microsecond,
            ]
        },
        "unbonding_period_days": 14,
        "claimed": False,
    }


def finances(*, locked=False):
    return {
        "self_bond": "0",
        "custody": "1" if locked else "0",
        "pending_total": "1" if locked else "0",
        "heap_size": 1 if locked else 0,
    }


def test_complete_real_preconditions_have_safe_public_witness():
    values = initial_fixture()
    result = checks.validate_initial_state(*values)
    assert result["nil_prevote_verified"] is True
    assert result["powers"] == {"A" * 40: 10, "B" * 40: 10, "C" * 40: 10}
    assert "signature" not in str(result).lower()


@pytest.mark.parametrize("wrong", ["app-power", "round", "proposer", "wrong-vote-round", "non-nil"])
def test_initial_precondition_does_not_accept_other_consensus_states(wrong):
    consensus, native, powers, nodes, low = initial_fixture()
    if wrong == "app-power":
        powers[nodes[0]["account"]] = 20
    elif wrong == "round":
        consensus["round_state"]["height/round/step"] = "1/1/6"
    elif wrong == "proposer":
        consensus["round_state"]["proposer"]["address"] = nodes[2]["address"]
    elif wrong == "wrong-vote-round":
        consensus["round_state"]["height_vote_set"][0]["round"] = 1
    else:
        consensus["round_state"]["height_vote_set"][0]["prevotes"][1] = consensus["round_state"][
            "height_vote_set"
        ][0]["prevotes"][1].replace("000000000000 ABCDEF", "111111111111 ABCDEF")
    with pytest.raises(Exception):
        checks.validate_initial_state(consensus, native, powers, nodes, low)


def test_stale_clock_proof_uses_actual_headers_and_exact_microsecond_anchor():
    headers = headers_fixture()
    now = checks.timestamp_nanoseconds("2026-04-11T00:00:00Z")
    result = checks.validate_stale_headers(
        headers, checks.timestamp_nanoseconds(headers[0]["time"]), now, now + 10**9
    )
    assert result["headers"] == headers
    anchor = checks.validate_anchor(locked_record(), 1, headers[1], 14)
    assert anchor["anchor_vm_nanos"] == now
    assert anchor["unlock_nanos"] == now + 14 * checks.DAY_NS


@pytest.mark.parametrize("wrong", ["height", "not-stale", "no-jump", "not-current"])
def test_stale_clock_preflight_cannot_be_replaced_by_a_different_clock_scenario(wrong):
    headers = headers_fixture()
    now = checks.timestamp_nanoseconds("2026-04-11T00:00:00Z")
    genesis = checks.timestamp_nanoseconds(headers[0]["time"])
    if wrong == "height":
        headers[2]["height"] = "4"
    elif wrong == "not-stale":
        now = genesis + checks.DAY_NS
    elif wrong == "no-jump":
        headers[1]["time"] = headers[0]["time"]
    else:
        headers[1]["time"] = "2026-04-10T00:00:00Z"
    with pytest.raises(Exception):
        checks.validate_stale_headers(headers, genesis, now, now + 10**9)


@pytest.mark.parametrize(
    "field,value", [("created_block", 2), ("unbonding_period_days", 0), ("claimed", True)]
)
def test_anchor_preserves_creation_captured_period_and_unclaimed_principal(field, value):
    record = locked_record()
    record[field] = value
    with pytest.raises(Exception):
        checks.validate_anchor(record, 1, headers_fixture()[1], 14)


def test_locked_financial_proof_accounts_for_the_live_heap_and_prior_principal():
    result = checks.validate_locked_finances(finances(), finances(locked=True), locked_record(), 1)
    assert result["locked_principal"] == "1"


@pytest.mark.parametrize(
    "field,value", [("self_bond", "1"), ("pending_total", "0"), ("heap_size", 0)]
)
def test_locked_financial_proof_cannot_ignore_principal_or_heap_entries(field, value):
    after = finances(locked=True)
    after[field] = value
    with pytest.raises(Exception):
        checks.validate_locked_finances(finances(), after, locked_record(), 1)


def test_public_summary_drops_unrelated_secret_or_raw_transaction_fields():
    record = {**locked_record(), "private_key": "hidden", "raw_tx": "hidden"}
    receipt = {
        "height": 1,
        "tx_hash": "A" * 64,
        "success": True,
        "transaction": {"private": "hidden"},
    }
    assert "hidden" not in str(checks.safe_record(record))
    assert checks.safe_receipt(receipt) == {"height": 1, "tx_hash": "A" * 64, "success": True}


class PositiveChain:
    def __init__(self, *, bad_zero=False, zero_failure=False):
        from types import SimpleNamespace

        self.owner = "a" * 64
        self.record = locked_record(owner=self.owner, created=10, anchor="2026-01-01T00:00:01Z")
        self.days = 14
        self.locked = False
        self.bad_zero = bad_zero
        self.zero_failure = zero_failure
        self.actions = []
        self.governance = []
        self.events = []
        self.funding_custody = 0
        self.voters_visible = False
        wallets = [SimpleNamespace(public_key=str(index) * 64) for index in range(5)]
        self.voter_balances = {wallet.public_key: 1250 for wallet in wallets[1:4]}
        self.runner = SimpleNamespace(
            nodes=[object()],
            founder_wallet=SimpleNamespace(public_key=self.owner),
            validator_wallets=wallets,
            fund_wallets=self.fund,
            submit_contract_call=self.send,
        )

    async def fund(self, session, wallets, *, amount):
        assert wallets == self.runner.validator_wallets[1:4]
        assert amount == 100_000
        self.events.append("fund-voters-and-await-peer-visibility")
        for wallet in wallets:
            self.voter_balances[wallet.public_key] = amount
        # Model the shared fund_wallets contract: it returns only after every
        # peer serves the funded balances. Funding fees affect custody first.
        self.funding_custody = 3
        self.voters_visible = True
        return []

    async def send(self, session, **kwargs):
        function = kwargs["function"]
        self.actions.append(function)
        assert "chi" not in kwargs, "Committed calls must use the SDK simulation budget"
        if function == "unbond_self":
            self.locked = True
        failed = function == "claim_unbond"
        if failed:
            assert kwargs["expect_success"] is False
            assert kwargs["expected_message_fragment"] == "locked"
        return {
            "height": 12 if failed else 10,
            "tx_hash": "a" * 64,
            "success": not failed,
            "transaction": "never public",
        }

    async def query(self, session, node, path):
        if path.endswith("config:unbonding_period_days"):
            return self.days
        if path.startswith("/validators_pending_unbonds_page/"):
            return {"unbonds": [self.record], "next_cursor": None}
        if "pending_unbonds:" in path:
            return self.record
        state = finances(locked=self.locked)
        state["custody"] = str(int(state["custody"]) + self.funding_custody)
        for field, part in [
            ("self_bond", "self_bond:"),
            ("custody", "balances:validators"),
            ("pending_total", "unbond_totals:"),
            ("heap_size", "unbond_heap_sizes:"),
        ]:
            if part in path:
                self.events.append("financial-snapshot")
                return state[field]
        raise AssertionError("unexpected public query")

    async def vote(self, runner, session, kind, arg, label):
        assert min(self.voter_balances.values()) >= 2500, "Governance voters are underfunded"
        assert self.voters_visible, "Voter funding is not committed across all peers"
        self.events.append("governance-vote")
        days = arg["unbonding_period_days"]
        self.governance.append(days)
        if days == 0 and self.zero_failure:
            raise RuntimeError("zero policy unavailable")
        self.days = days
        if days == 0 and self.bad_zero:
            self.record["unbonding_period_days"] = 0
        return {"proposal_id": len(self.governance), "public": True}

    async def rpc(self, session, node, method, **params):
        assert method == "block"
        return {
            "block": {
                "header": {
                    "height": str(params["height"]),
                    "time": "2026-01-01T00:00:01.000000456Z",
                }
            }
        }


def patch_positive_chain(monkeypatch, chain):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(checks, "query", chain.query)
    monkeypatch.setattr(checks, "rpc", chain.rpc)
    monkeypatch.setattr(checks, "governance_vote", chain.vote)
    monkeypatch.setattr(checks, "wait_height", AsyncMock())


def test_positive_period_harness_generates_real_anchor_work_and_restores_policy(monkeypatch):
    import asyncio

    chain = PositiveChain()
    patch_positive_chain(monkeypatch, chain)
    proof = asyncio.run(checks.positive_unbond_rehearsal(chain.runner, object()))
    assert chain.actions == ["approve", "bond_self", "unbond_self", "claim_unbond", "claim_unbond"]
    assert chain.governance == [0, 14] and chain.days == 14
    assert (
        chain.events.index("fund-voters-and-await-peer-visibility")
        < chain.events.index("financial-snapshot")
        < chain.events.index("governance-vote")
    )
    assert proof["financial"]["before"]["custody"] == "3"
    assert proof["financial"]["after"]["custody"] == "4"
    assert proof["record"]["unbonding_period_days"] == 14
    assert proof["anchor"]["anchor_height"] == 11
    assert proof["financial"]["after"]["heap_size"] == 1
    assert proof["restored_policy"] == proof["original_policy"] == {"unbonding_period_days": 14}
    assert "never public" not in str(proof)


@pytest.mark.parametrize("failure", ["shortened-lock", "governance"])
def test_positive_period_harness_always_restores_policy_on_a_real_failure(monkeypatch, failure):
    import asyncio

    chain = PositiveChain(
        bad_zero=failure == "shortened-lock", zero_failure=failure == "governance"
    )
    patch_positive_chain(monkeypatch, chain)
    with pytest.raises(Exception, match="captured unbond|zero policy unavailable"):
        asyncio.run(checks.positive_unbond_rehearsal(chain.runner, object()))
    assert chain.governance == [0, 14] and chain.days == 14


def test_fixture_uses_only_canonical_three_equal_power_genesis_and_private_homes(
    tmp_path, monkeypatch
):
    import xian.genesis_builder as builder
    import xian.node_setup as setup

    captured = {}
    homes = []

    def canonical(**kwargs):
        captured.update(kwargs)
        return {
            "chain_id": kwargs["chain_id"],
            "genesis_time": kwargs["genesis_time"],
            "abci_genesis": {"genesis": [{"key": "__fee_policy", "value": kwargs["fee_policy"]}]},
        }

    monkeypatch.setattr(builder, "build_local_network_genesis", canonical)
    monkeypatch.setattr(checks, "rebase_test_evidence_window", lambda genesis, *args: genesis)
    monkeypatch.setattr(setup, "materialize_cometbft_home", lambda **kwargs: homes.append(kwargs))
    fixture = checks.prepare_fixture(tmp_path / "owned", "image-id")
    assert [v["power"] for v in captured["validators"]] == [10, 10, 10]
    assert captured["validator_constructor_overrides"] == {"unbonding_period_days": 14}
    assert fixture.fee_policy["max_block_chi"] == 500000
    assert fixture.low["address"] != min(node["address"] for node in fixture.nodes)
    assert len(homes) == 3 and all(
        item["config"]["consensus"]["timeout_commit"] == "8s" for item in homes
    )
    assert all(item["config"]["consensus"]["skip_timeout_commit"] is False for item in homes)
    for item in homes:
        p2p = item["config"]["p2p"]
        assert not p2p["seeds"] and not p2p["persistent_peers"] and p2p["pex"] is False
        assert item["config"]["rpc"]["unsafe"] is True
    assert (fixture.output.stat().st_mode & 0o777) == 0o700
    assert ((fixture.output / "private").stat().st_mode & 0o777) == 0o700
    for name in fixture.nodes:
        assert name["container"].startswith("xian-idle-unbond-")


def test_fixture_refuses_existing_data_instead_of_reusing_a_chain(tmp_path):
    with pytest.raises(Exception, match="fresh isolated"):
        checks.prepare_fixture(tmp_path, "image")


def test_private_files_are_owner_only_and_do_not_overwrite_existing_evidence(tmp_path):
    import json

    target = tmp_path / "private" / "details.json"
    checks.private_json(target, {"public": "test"})
    assert (target.stat().st_mode & 0o777) == 0o600
    assert json.loads(target.read_text()) == {"public": "test"}
    with pytest.raises(FileExistsError):
        checks.private_json(target, {"replaced": True})


def test_setup_is_four_real_signed_transactions_with_one_contiguous_sender():
    import json
    from types import SimpleNamespace

    fixture = SimpleNamespace(genesis={"chain_id": "fixture"}, founder=checks.Wallet())
    transactions = checks.prepared_setup(fixture)
    payloads = [json.loads(tx.json_bytes)["payload"] for tx in transactions]
    assert [p["nonce"] for p in payloads] == [0, 1, 2, 3]
    assert [p["function"] for p in payloads] == [
        "approve",
        "bond_self",
        "unbond_self",
        "claim_unbond",
    ]
    assert sum(p["chi_supplied"] for p in payloads[:3]) == 300000
    assert all(p["sender"] == fixture.founder.public_key for p in payloads)


def receipt_record(*, height=1, code=0, result=None):
    return {
        "height": str(height),
        "hash": "A" * 64,
        "tx_result": {"code": code, "data": {"result": result}},
    }


def test_actual_h1_receipts_must_show_deferred_positive_unbond():
    record = {**locked_record(), "unlock_at": None}
    values = [receipt_record(), receipt_record(), receipt_record(result=record)]
    assert checks.checked_h1_unbond(values) == record


@pytest.mark.parametrize(
    "wrong", ["height", "failure", "not-deferred", "wrong-id", "no-positive-period"]
)
def test_stale_preflight_refuses_a_different_setup_outcome(wrong):
    record = {**locked_record(), "unlock_at": None}
    values = [receipt_record(), receipt_record(), receipt_record(result=record)]
    if wrong == "height":
        values[2]["height"] = "2"
    elif wrong == "failure":
        values[0]["tx_result"]["code"] = 1
    elif wrong == "not-deferred":
        record["unlock_at"] = locked_record()["unlock_at"]
    elif wrong == "wrong-id":
        record["id"] = 2
    else:
        record["unbonding_period_days"] = 0
    with pytest.raises(Exception):
        checks.checked_h1_unbond(values)


def test_actual_h3_failure_must_be_maturity_denial_not_a_gas_or_nonce_failure():
    result = checks.checked_h3_claim(
        receipt_record(height=3, code=1, result="Unbond is still locked.")
    )
    assert result == {"height": 3, "tx_hash": "A" * 64, "success": False, "immature": True}


@pytest.mark.parametrize(
    "height,code,message", [(4, 1, "locked"), (3, 0, "success"), (3, 1, "out of chi")]
)
def test_claim_timing_and_real_execution_reason_are_mandatory(height, code, message):
    with pytest.raises(Exception):
        checks.checked_h3_claim(receipt_record(height=height, code=code, result=message))


def test_execution_data_supports_actual_base64_typed_wire():
    import base64
    import json

    receipt = receipt_record(result=None)
    receipt["tx_result"]["data"] = base64.b64encode(
        json.dumps({"result": {"id": 1}}).encode()
    ).decode()
    assert checks.execution_result(receipt) == {"result": {"id": 1}}


def test_bounded_page_discovery_uses_actual_created_height_and_owner(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    record = locked_record(created=10)
    pages = [
        {"unbonds": [{**record, "owner": "other"}], "next_cursor": 2},
        {"unbonds": [record], "next_cursor": None},
    ]
    query = AsyncMock(side_effect=pages)
    monkeypatch.setattr(checks, "query", query)
    assert asyncio.run(checks.created_unbond(object(), object(), record["owner"], 10)) == record
    assert query.await_args_list[1].args[-1].endswith("/cursor=2")


@pytest.mark.parametrize(
    "page", [{"unbonds": [], "next_cursor": 1}, {"unbonds": [], "next_cursor": None}]
)
def test_unbond_discovery_rejects_repeated_cursor_or_missing_record(monkeypatch, page):
    import asyncio
    from unittest.mock import AsyncMock

    monkeypatch.setattr(checks, "query", AsyncMock(return_value=page))
    with pytest.raises(Exception, match="cursor repeated|exactly one"):
        asyncio.run(checks.created_unbond(object(), object(), "owner", 10))


def test_anchor_poll_requires_actual_nonnull_record_and_has_a_real_deadline(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    record = locked_record()
    query = AsyncMock(side_effect=[None, {**record, "unlock_at": None}, record])
    monkeypatch.setattr(checks, "query", query)
    monkeypatch.setattr(checks.asyncio, "sleep", AsyncMock())
    assert asyncio.run(checks.anchored_record(object(), object(), 1)) == record
    monkeypatch.setattr(checks, "query", AsyncMock(return_value=None))
    monkeypatch.setattr(
        checks, "time", __import__("types").SimpleNamespace(monotonic=iter([0, 2]).__next__)
    )
    with pytest.raises(Exception, match="did not appear"):
        asyncio.run(checks.anchored_record(object(), object(), 1, timeout=1))


def test_poll_handles_rpc_startup_without_claiming_success_from_timeout(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    monkeypatch.setattr(checks.asyncio, "sleep", AsyncMock())
    operation = AsyncMock(side_effect=[OSError("not listening"), {"ready": False}, {"ready": True}])
    assert asyncio.run(checks.poll(operation, lambda value: value["ready"], "fixture")) == {
        "ready": True
    }
    monkeypatch.setattr(
        checks, "time", __import__("types").SimpleNamespace(monotonic=iter([0, 2]).__next__)
    )
    with pytest.raises(Exception, match="deadline"):
        asyncio.run(checks.poll(AsyncMock(), lambda _: True, "fixture", timeout=1))


def test_mempool_witness_requires_all_three_actual_signed_tx_hashes(monkeypatch):
    import asyncio
    import base64
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    fixture = SimpleNamespace(genesis={"chain_id": "fixture"}, founder=checks.Wallet())
    transactions = checks.prepared_setup(fixture)[:3]
    rpc = AsyncMock(
        return_value={"txs": [base64.b64encode(tx.submitted_bytes).decode() for tx in transactions]}
    )
    monkeypatch.setattr(checks, "rpc", rpc)
    assert asyncio.run(checks.require_mempool(object(), object(), transactions)) == sorted(
        tx.tx_hash for tx in transactions
    )
    rpc.return_value = {"txs": []}
    with pytest.raises(Exception, match="all in.*mempool"):
        asyncio.run(checks.require_mempool(object(), object(), transactions))


def test_cleanup_attempts_every_owned_resource_even_after_log_failure(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    calls = []

    async def command(*args, **kwargs):
        calls.append(args)
        if args[1] == "logs":
            raise OSError("log unavailable")
        return ""

    monkeypatch.setattr(checks, "command", command)
    fixture = SimpleNamespace(output=tmp_path, network="owned-network")
    with pytest.raises(Exception, match="cleanup"):
        asyncio.run(
            checks.cleanup_fixture(fixture, [{"container": "owned-a"}, {"container": "owned-b"}])
        )
    assert ("docker", "rm", "-f", "-v", "owned-a") in calls
    assert ("docker", "rm", "-f", "-v", "owned-b") in calls
    assert calls[-1] == ("docker", "network", "rm", "owned-network")


def test_live_case_stages_each_mempool_before_connecting_validators(monkeypatch):
    import asyncio
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    founder = checks.Wallet()
    nodes = [{"container": f"owned-{i}", "node_id": f"{i:040x}"} for i in range(3)]
    fixture = SimpleNamespace(
        nodes=nodes,
        low=nodes[1],
        founder=founder,
        genesis={"chain_id": "fixture"},
        session=object(),
        wall_start=checks.timestamp_nanoseconds("2026-04-11T00:00:00Z"),
        genesis_nanos=checks.timestamp_nanoseconds("2026-01-01T00:00:00Z"),
        fee_policy={"max_tx_chi": 200000, "max_block_chi": 500000},
    )
    events = []
    pending = {}
    witnessed = set()

    async def start(fixture, node, created):
        events.append("start-" + node["container"])
        created.append(node)
        pending[node["container"]] = []
        return SimpleNamespace(moniker=node["container"])

    async def initial(*args):
        events.append("nil")
        return {"nil_prevote_verified": True}

    async def broadcast(session, node, tx):
        pending[node.moniker].append(tx)
        events.append(f"broadcast-{node.moniker}-{len(pending[node.moniker])}")

    async def mempool(session, node, txs):
        assert pending[node.moniker] == txs and len(txs) == 3
        witnessed.add(node.moniker)
        return [tx.tx_hash for tx in txs]

    async def rpc(session, node, method, **params):
        if method == "dial_peers":
            assert witnessed == {member["container"] for member in nodes}
            assert set(json.loads(params["peers"])) == {
                f"{member['node_id']}@{member['container']}:26656"
                for member in nodes
                if member["container"] != node.moniker
            }
            assert params["persistent"] == "true"
            events.append("dial-" + node.moniker)
            return {}
        assert method == "commit" and params == {"height": 1}
        return {"signed_header": {"commit": {"round": 1}}}

    async def block(session, node, h):
        events.append(f"block-{h}")
        return {"block": {"header": headers_fixture()[h - 1]}}

    async def receipt(session, node, tx):
        index = pending[node.moniker].index(tx)
        if index == 2:
            return receipt_record(
                result={**locked_record(owner=founder.public_key), "unlock_at": None}
            )
        if index == 3:
            return receipt_record(height=3, code=1, result="Unbond is still locked.")
        return receipt_record()

    monkeypatch.setattr(checks, "start_fixture_node", start)
    monkeypatch.setattr(checks, "initial_precondition", initial)
    monkeypatch.setattr(checks, "broadcast", broadcast)
    monkeypatch.setattr(checks, "require_mempool", mempool)
    monkeypatch.setattr(checks, "block_at", block)
    monkeypatch.setattr(checks, "transaction_receipt", receipt)
    monkeypatch.setattr(checks, "wait_height", AsyncMock())
    monkeypatch.setattr(checks, "rpc", rpc)
    monkeypatch.setattr(
        checks, "anchored_record", AsyncMock(return_value=locked_record(owner=founder.public_key))
    )
    monkeypatch.setattr(
        checks, "financial_state", AsyncMock(side_effect=[finances(), finances(locked=True)])
    )
    monkeypatch.setattr(
        checks, "three_node_agreement", AsyncMock(return_value={"height": 4, "app_hash": "public"})
    )
    monkeypatch.setattr(checks, "security_checkpoint_proof", AsyncMock(return_value={"height": 4}))
    monkeypatch.setattr(
        checks, "coupled_evidence_rehearsal", AsyncMock(return_value={"protected_h1": True})
    )
    capture = AsyncMock(return_value={"height": 14, "archive_sha256": "public-digest"})
    monkeypatch.setattr(checks, "capture_preflight_corpus", capture, raising=False)
    proof = asyncio.run(checks.execute_stale_case(fixture, []))
    for member in nodes:
        moniker = member["container"]
        assert (
            events.index("nil")
            < events.index(f"broadcast-{moniker}-1")
            < events.index(f"dial-{moniker}")
            < events.index("block-2")
        )
    assert events.index("block-2") < events.index("broadcast-owned-1-4")
    assert pending["owned-0"] == pending["owned-1"][:3] == pending["owned-2"]
    assert proof["h1_commit_round"] == 1
    assert proof["early_claim"]["height"] == 3 and proof["financial"]["locked_principal"] == "1"
    assert proof["consensus"]["height"] == 4
    capture.assert_awaited_once_with(fixture)
    assert proof["replay_corpus"]["height"] == 14
    assert "private_key" not in str(proof) and "submitted_bytes" not in str(proof)


def test_preflight_orchestration_uses_only_owned_runtime_and_always_cleans_up(
    tmp_path, monkeypatch
):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    fixture = SimpleNamespace(output=tmp_path, network="owned-network")
    prepare = __import__("unittest.mock", fromlist=["Mock"]).Mock(return_value=fixture)
    monkeypatch.setattr(checks, "prepare_fixture", prepare)
    command = AsyncMock()
    monkeypatch.setattr(checks, "command", command)
    execute = AsyncMock(return_value={"ok": True, "safe": True})
    monkeypatch.setattr(checks, "execute_stale_case", execute)
    cleanup = AsyncMock()
    monkeypatch.setattr(checks, "cleanup_fixture", cleanup)
    proof = asyncio.run(checks.stale_unbond_preflight(image="image", output_dir=tmp_path))
    assert proof == {"ok": True, "safe": True}
    command.assert_awaited_once_with("docker", "network", "create", "owned-network")
    cleanup.assert_awaited_once_with(fixture, [])
    assert (tmp_path / "public-proof.json").is_file()
    execute.side_effect = RuntimeError("fixture precondition failed")
    with pytest.raises(RuntimeError, match="fixture precondition"):
        asyncio.run(checks.stale_unbond_preflight(image="image", output_dir=tmp_path / "second"))
    assert cleanup.await_count == 2


def test_startup_records_owned_container_and_waits_for_transaction_readiness(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    node = {"home": tmp_path, "container": "owned", "account": "a" * 64, "index": 0}
    created = []
    live = SimpleNamespace(moniker="owned")
    monkeypatch.setattr(checks, "create_observer", AsyncMock(return_value=live))
    catching_up = iter([True, False])
    observed = []

    async def status(session, node, method, **kwargs):
        assert created and method == "status"
        observed.append(next(catching_up))
        return {"sync_info": {"catching_up": observed[-1]}}

    monkeypatch.setattr(checks, "rpc", status)
    fixture = SimpleNamespace(image="image", network="owned-network", session=object(), running={})
    assert asyncio.run(checks.start_fixture_node(fixture, node, created)) is live
    assert created == [node]
    assert observed == [True, False]
    assert live.abci_container == "owned"


@pytest.mark.parametrize("wrong", [None, "native-power", "proposer", "vote", "app-power"])
def test_initial_rpc_asserts_actual_fee_caps_and_actual_native_application_powers(
    monkeypatch, wrong
):
    import asyncio
    from types import SimpleNamespace

    consensus, native, powers, nodes, low = initial_fixture()
    dump = {
        "round_state": {
            "height": "1",
            "round": 0,
            "step": 4,
            "validators": {**native, "proposer": native["validators"][0]},
            "votes": consensus["round_state"]["height_vote_set"],
        }
    }
    policy = {"max_tx_chi": 200000, "max_block_chi": 500000, "tx_fee_mode": "paid_metered"}
    if wrong == "native-power":
        dump["round_state"]["validators"]["validators"][0]["voting_power"] = "20"
    elif wrong == "proposer":
        dump["round_state"]["validators"]["proposer"] = native["validators"][2]
    elif wrong == "vote":
        prevotes = dump["round_state"]["votes"][0]["prevotes"]
        prevotes[1] = prevotes[1].replace("000000000000 ABCDEF", "111111111111 ABCDEF")
    elif wrong == "app-power":
        powers[nodes[0]["account"]] = 20

    async def query(session, node, path):
        if path == "/get/__fee_policy":
            return policy
        return powers[path.rsplit(":", 1)[1]]

    async def rpc(session, node, method, **kwargs):
        assert method == "dump_consensus_state", "Pre-H1 validators RPC requests a future height"
        assert kwargs == {}
        return dump

    monkeypatch.setattr(checks, "query", query)
    monkeypatch.setattr(checks, "rpc", rpc)
    fixture = SimpleNamespace(fee_policy=policy, nodes=nodes, low=low, session=object())
    if wrong is not None:
        with pytest.raises(Exception, match="powers|proposer|NIL"):
            asyncio.run(checks.initial_precondition(fixture, object()))
        return
    result = asyncio.run(checks.initial_precondition(fixture, object()))
    assert result["nil_prevote_verified"]
    fixture.fee_policy = {**policy, "max_block_chi": 200000}
    with pytest.raises(Exception, match="fee caps"):
        asyncio.run(checks.initial_precondition(fixture, object()))
    policy["max_block_chi"] = 200000
    with pytest.raises(Exception, match="chi caps"):
        asyncio.run(checks.initial_precondition(fixture, object()))


def test_stock_full_round_state_normalizes_integer_wire_forms_without_faking_the_step():
    dump = {"round_state": {"height": "1", "round": 0, "step": 4}}
    assert checks.dump_round_step(dump) == "1/0/4"
    dump["round_state"]["step"] = 3
    assert checks.dump_round_step(dump) == "1/0/3"
    dump["round_state"].update(height="2", round=1, step="4")
    assert checks.dump_round_step(dump) == "2/1/4"


@pytest.mark.parametrize(
    "representation",
    ["sdk", "naive", "aware", "tagged", "native", "iso-space", "iso-t", "iso-aware"],
)
def test_contract_clock_decodes_real_sdk_wire_and_stdlib_forms_exactly(representation):
    from datetime import UTC, datetime, timedelta, timezone

    from xian_py.xian_async import decode_wire_numbers
    from xian_runtime_types.time import Datetime

    tag = {"__time__": [2026, 1, 1, 0, 0, 1, 123456]}
    decoded = decode_wire_numbers(tag)
    values = {
        "sdk": decoded,
        "naive": datetime(2026, 1, 1, 0, 0, 1, 123456),
        "aware": datetime(2026, 1, 1, 2, 0, 1, 123456, tzinfo=timezone(timedelta(hours=2))),
        "tagged": tag,
        "native": Datetime(2026, 1, 1, 0, 0, 1, 123456),
        "iso-space": "2026-01-01 00:00:01.123456",
        "iso-t": "2026-01-01T00:00:01.123456",
        "iso-aware": "2026-01-01T02:00:01.123456+02:00",
    }
    expected = 1767225601123456000
    assert checks.contract_time_nanos(values[representation]) == expected
    header = {"height": "2", "time": "2025-12-18T00:00:01.123456789Z"}
    record = locked_record(anchor="2025-12-18T00:00:01.123456Z")
    record["unlock_at"] = values[representation]
    assert checks.validate_anchor(record, 1, header, 14)["unlock_nanos"] == expected
    with pytest.raises(ValueError, match="RFC3339"):
        checks.timestamp_nanoseconds(
            datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None).isoformat()
        )


def test_naive_contract_datetime_is_utc_even_in_a_non_utc_host_subprocess():
    import os
    import subprocess

    code = """import time
time.tzset()
from datetime import datetime
from localnet_idle_unbond_checks import contract_time_nanos
assert contract_time_nanos(datetime(2026,1,1,0,0,1,123456)) == 1767225601123456000
assert contract_time_nanos('2026-01-01 00:00:01.123456') == 1767225601123456000
assert contract_time_nanos('2026-01-01T00:00:01.123456') == 1767225601123456000
"""
    env = dict(os.environ, TZ="Pacific/Auckland")
    env["PYTHONPATH"] = str(Path(checks.__file__).parent) + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_actual_node_query_encoder_roundtrip_preserves_exact_contract_clock(monkeypatch):
    import asyncio
    import base64
    import json
    from unittest.mock import AsyncMock

    import localnet_chain_checks
    from xian.methods.query import _encode_query_value
    from xian_runtime_types.time import Datetime

    stored = locked_record(anchor="2025-12-18T00:00:01.123456Z")
    stored["unlock_at"] = Datetime(2026, 1, 1, 0, 0, 1, 123456)
    encoded, kind = _encode_query_value(stored)
    assert kind == "dict"
    assert json.loads(encoded)["unlock_at"] == "2026-01-01 00:00:01.123456"
    response = {
        "code": 0,
        "info": kind,
        "value": base64.b64encode(encoded).decode(),
    }
    rpc = AsyncMock(return_value={"response": response})
    monkeypatch.setattr(localnet_chain_checks, "rpc", rpc)
    record = asyncio.run(
        localnet_chain_checks.query("session", "node", "/get/validators.pending_unbonds:1")
    )
    assert checks.contract_time_nanos(record["unlock_at"]) == 1767225601123456000
    proof = checks.validate_anchor(
        record, 1, {"height": "2", "time": "2025-12-18T00:00:01.123456789Z"}, 14
    )
    assert proof["unlock_nanos"] == 1767225601123456000
    rpc.assert_awaited_once_with(
        "session", "node", "abci_query", path=json.dumps("/get/validators.pending_unbonds:1")
    )


@pytest.mark.parametrize("microseconds", [1, 123457, 999999])
def test_contract_datetime_microseconds_never_round_through_binary_float(microseconds):
    from datetime import datetime

    value = datetime(2026, 1, 1, 0, 0, 1, microseconds)
    assert checks.contract_time_nanos(value) == 1767225601000000000 + microseconds * 1000


def test_three_native_peers_must_agree_on_post_claim_header_root_and_exact_results(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    monkeypatch.setattr(checks, "wait_height", AsyncMock())

    async def rpc(session, node, method, **kwargs):
        return (
            {"block_id": {"hash": "block"}, "block": {"header": {"app_hash": "root"}}}
            if method == "block"
            else {"txs_results": []}
        )

    monkeypatch.setattr(checks, "rpc", rpc)
    result = asyncio.run(checks.three_node_agreement(object(), [object(), object(), object()]))
    assert result["height"] == 4 and result["app_hash"] == "root"
    first = object()

    async def bad(session, node, method, **kwargs):
        value = await rpc(session, node, method, **kwargs)
        if method == "block" and node is first:
            value["block_id"]["hash"] = "different"
        return value

    monkeypatch.setattr(checks, "rpc", bad)
    with pytest.raises(Exception, match="diverged"):
        asyncio.run(checks.three_node_agreement(object(), [first, object(), object()]))


def test_early_claim_must_have_a_real_failed_committed_receipt(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    runner = SimpleNamespace(
        founder_wallet=object(),
        submit_contract_call=AsyncMock(return_value={"success": False, "height": None}),
    )
    with pytest.raises(Exception, match="actual failed block"):
        asyncio.run(checks.early_claim(runner, object(), 1))


def test_exact_numbers_and_contract_datetime_normalization():
    assert str(checks.number({"__fixed__": "0.125"})) == "0.125"
    assert str(checks.number(None)) == "0"
    assert checks.contract_time_nanos("1970-01-01T00:00:01.000001Z") == 1000001000


def test_created_unbond_discovery_fails_explicitly_at_its_page_budget(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        checks,
        "query",
        AsyncMock(side_effect=[{"unbonds": [], "next_cursor": i} for i in range(1, 11)]),
    )
    with pytest.raises(Exception, match="page budget"):
        asyncio.run(checks.created_unbond(object(), object(), "owner", 10))


def test_cleanup_collects_log_evidence_privately_and_removes_owned_runtime(tmp_path, monkeypatch):
    import asyncio
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    command = AsyncMock(return_value="private diagnostics")
    monkeypatch.setattr(checks, "command", command)
    asyncio.run(
        checks.cleanup_fixture(
            SimpleNamespace(output=tmp_path, network="owned"), [{"container": "owned-one"}]
        )
    )
    log = tmp_path / "private/owned-one.log.json"
    assert json.loads(log.read_text()) == {"log": "private diagnostics"}
    assert (log.stat().st_mode & 0o777) == 0o600
    assert command.await_args.args == ("docker", "network", "rm", "owned")


@pytest.mark.parametrize("operation", ["rm", "network"])
def test_cleanup_reports_real_container_or_network_removal_failure(
    tmp_path, monkeypatch, operation
):
    import asyncio
    from types import SimpleNamespace

    async def command(*args, **kwargs):
        if args[1] == operation:
            raise OSError("cleanup failed")
        return "log"

    monkeypatch.setattr(checks, "command", command)
    with pytest.raises(Exception, match="cleanup"):
        asyncio.run(
            checks.cleanup_fixture(
                SimpleNamespace(output=tmp_path, network="owned"), [{"container": "owned"}]
            )
        )


def test_rpc_wait_wrappers_use_the_actual_tx_hash_and_block_height(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    rpc = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(checks, "rpc", rpc)
    assert asyncio.run(
        checks.transaction_receipt("session", "node", SimpleNamespace(tx_hash="AB"))
    ) == {"ok": True}
    rpc.assert_awaited_once_with("session", "node", "tx", hash="0xAB")
    assert asyncio.run(checks.block_at("session", "node", 2)) == {"ok": True}
    assert rpc.await_args.kwargs == {"height": 2}


def test_native_claim_result_divergence_is_not_hidden_by_matching_roots(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    monkeypatch.setattr(checks, "wait_height", AsyncMock())
    first = object()

    async def rpc(session, node, method, **kwargs):
        if method == "block":
            return {"block_id": {"hash": "same"}, "block": {"header": {"app_hash": "same"}}}
        return {"txs_results": [{"code": 1 if node is first else 0}]}

    monkeypatch.setattr(checks, "rpc", rpc)
    with pytest.raises(Exception, match="different claim receipts"):
        asyncio.run(checks.three_node_agreement(object(), [first, object(), object()]))


def test_cli_uses_the_same_async_core_without_a_second_runtime_path(tmp_path, monkeypatch, capsys):
    import json
    from unittest.mock import AsyncMock

    core = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(checks, "stale_unbond_preflight", core)
    monkeypatch.setattr(
        sys, "argv", ["preflight", "--image", "image-id", "--output-dir", str(tmp_path)]
    )
    checks.main()
    core.assert_awaited_once_with(image="image-id", output_dir=tmp_path)
    assert json.loads(capsys.readouterr().out) == {
        "ok": True,
        "proof": str(tmp_path / "public-proof.json"),
    }


def test_final_record_check_allows_unrelated_later_staking_custody_changes(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    record = locked_record()
    proof = {"owner": record["owner"], "unbond_id": 1, "record": checks.safe_record(record)}
    monkeypatch.setattr(checks, "anchored_record", AsyncMock(return_value=record))
    monkeypatch.setattr(
        checks, "query", AsyncMock(return_value={"unbonds": [record], "next_cursor": None})
    )
    financial = AsyncMock(
        side_effect=AssertionError("Final verification must not compare global custody")
    )
    monkeypatch.setattr(checks, "financial_state", financial)
    result = asyncio.run(
        checks.verify_positive_record(SimpleNamespace(nodes=[object()]), object(), proof)
    )
    assert result == {"record": proof["record"], "owner_pending_index_verified": True}
    financial.assert_not_awaited()


def test_final_record_check_requires_real_owner_pending_index_membership(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    record = locked_record()
    proof = {"owner": record["owner"], "unbond_id": 1, "record": checks.safe_record(record)}
    monkeypatch.setattr(checks, "anchored_record", AsyncMock(return_value=record))
    monkeypatch.setattr(
        checks, "query", AsyncMock(return_value={"unbonds": [], "next_cursor": None})
    )
    with pytest.raises(Exception, match="pending unbond index"):
        asyncio.run(
            checks.verify_positive_record(SimpleNamespace(nodes=[object()]), object(), proof)
        )


def test_stale_jump_records_real_security_epoch_rotation_without_claiming_fast_block_history(
    monkeypatch,
):
    import asyncio
    from unittest.mock import AsyncMock

    header = headers_fixture()[1]
    schedule = {
        "security_checkpoint_height": 2,
        "previous_security_checkpoint_height": 0,
        "security_checkpoint_nanos": checks.timestamp_nanoseconds(header["time"]),
    }
    evidence = {
        "max_age_num_blocks": "100000",
        "max_age_duration": "1296000000000000",
        "max_bytes": "1048576",
    }
    state = {"height": 4, "schedule": schedule, "evidence": evidence, "block_cutoff_height": -99996}
    monkeypatch.setattr(checks, "retention_sample", AsyncMock(return_value=state))
    monkeypatch.setattr(
        checks, "rpc", AsyncMock(return_value={"consensus_params": {"evidence": evidence}})
    )
    result = asyncio.run(checks.security_checkpoint_proof(object(), object(), header))
    assert result["height"] == 4 and result["schedule"]["previous_security_checkpoint_height"] == 0
    assert result["effective_evidence"]["max_age_num_blocks"] == "100000"


@pytest.mark.parametrize("wrong", ["lost-zero", "wrong-height", "clock", "native-window"])
def test_security_rotation_proof_rejects_wrong_epoch_or_native_parameters(monkeypatch, wrong):
    import asyncio
    from unittest.mock import AsyncMock

    header = headers_fixture()[1]
    schedule = {
        "security_checkpoint_height": 2,
        "previous_security_checkpoint_height": 0,
        "security_checkpoint_nanos": checks.timestamp_nanoseconds(header["time"]),
    }
    evidence = {
        "max_age_num_blocks": "100000",
        "max_age_duration": "1296000000000000",
        "max_bytes": "1048576",
    }
    native = {"consensus_params": {"evidence": dict(evidence)}}
    if wrong == "lost-zero":
        schedule["previous_security_checkpoint_height"] = None
    elif wrong == "wrong-height":
        schedule["security_checkpoint_height"] = 3
    elif wrong == "clock":
        schedule["security_checkpoint_nanos"] += 1
    else:
        native["consensus_params"]["evidence"]["max_age_num_blocks"] = "1"
    state = {"height": 4, "schedule": schedule, "evidence": evidence, "block_cutoff_height": -99996}
    monkeypatch.setattr(checks, "retention_sample", AsyncMock(return_value=state))
    monkeypatch.setattr(checks, "rpc", AsyncMock(return_value=native))
    with pytest.raises(Exception, match="epoch|clock|window"):
        asyncio.run(checks.security_checkpoint_proof(object(), object(), header))


def test_small_evidence_window_rebuilds_genesis_through_canonical_driver_and_signing_apis(
    tmp_path, monkeypatch
):
    import dataclasses

    import contracting.storage.driver as storage
    import xian.evidence_policy as policies
    import xian.genesis_builder as builder

    saved = {}
    events = []
    raw = {"version": 2, "max_age_num_blocks": 100000, "other_security_field": "preserved"}
    genesis = {
        "chain_id": "local",
        "genesis_time": "2026-01-01T00:00:00Z",
        "validators": [{"power": "10"}],
        "abci_genesis": {
            "number": "0",
            "nanos": 123,
            "genesis": [
                {"key": "__evidence_policy", "value": raw},
                {"key": "currency.balances:a", "value": 7},
            ],
        },
    }

    class Driver:
        def __init__(self, **kwargs):
            events.append("driver")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            events.append("close")

        def set(self, key, value):
            saved[key] = value

        def get(self, key):
            return saved[key]

    @dataclasses.dataclass(frozen=True)
    class Policy:
        max_age_num_blocks: int
        raw: dict

        @classmethod
        def from_state(cls, value):
            return cls(value["max_age_num_blocks"], dict(value))

        def to_state(self):
            return {**self.raw, "max_age_num_blocks": self.max_age_num_blocks}

    def finalize(driver, **kwargs):
        events.append("canonical-finalize")
        assert kwargs == {"founder_private_key": "fixture-only", "number": "0", "nanos": 123}
        return {"hash": "recomputed-and-resigned"}

    def build(**kwargs):
        events.append("canonical-native-genesis")
        assert kwargs["abci_genesis"] == {"hash": "recomputed-and-resigned"}
        assert kwargs["validators"] == genesis["validators"]
        return {"verified": "rebuilt"}

    monkeypatch.setattr(storage, "Driver", Driver)
    monkeypatch.setattr(policies, "EvidencePolicy", Policy)
    monkeypatch.setattr(builder, "finalize_genesis_block", finalize)
    monkeypatch.setattr(builder, "build_cometbft_genesis", build)
    assert checks.rebase_test_evidence_window(genesis, "fixture-only", tmp_path) == {
        "verified": "rebuilt"
    }
    assert events == ["driver", "canonical-finalize", "close", "canonical-native-genesis"]
    assert saved["__evidence_policy"] == {**raw, "max_age_num_blocks": 5}
    assert raw["max_age_num_blocks"] == 100000 and saved["currency.balances:a"] == 7


def evidence_state():
    return {
        "height": 9,
        "last_block_nanos": 100 * checks.DAY_NS,
        "schedule": {"max_age_num_blocks": 5},
        "evidence": {
            "max_age_num_blocks": "9",
            "max_age_duration": str(15 * checks.DAY_NS),
            "max_bytes": "1048576",
        },
    }


def test_coupled_case_really_exceeds_both_nominal_expiry_dimensions():
    proof = checks.validate_coupled_evidence_ages(evidence_state(), 0)
    assert proof["age_blocks"] == 8 and proof["age_blocks"] > proof["nominal_block_window"]
    assert proof["age_nanos"] > int(proof["effective_evidence"]["max_age_duration"])


@pytest.mark.parametrize("wrong", ["base", "young-blocks", "young-time", "unprotected"])
def test_coupled_case_rejects_a_scenario_that_does_not_exercise_the_real_gap(wrong):
    state = evidence_state()
    if wrong == "base":
        state["schedule"]["max_age_num_blocks"] = 100000
    elif wrong == "young-blocks":
        state["height"] = 6
    elif wrong == "young-time":
        state["last_block_nanos"] = 15 * checks.DAY_NS
    else:
        state["evidence"]["max_age_num_blocks"] = "5"
    with pytest.raises(Exception):
        checks.validate_coupled_evidence_ages(state, 0)


def slash_states():
    before = {"financial": finances(locked=True), "dao_balance": "3"}
    after = {
        "financial": {"self_bond": "0", "custody": "0.95", "pending_total": "0.95", "heap_size": 1},
        "dao_balance": "3.05",
    }
    record = {**locked_record(), "amount": "0.95"}
    return before, after, record


def test_historical_slash_is_exact_and_does_not_release_the_remaining_principal():
    before, after, record = slash_states()
    result = checks.validate_pending_slash(before, after, record)
    assert result["slashed"] == "0.05" and result["remaining_record"]["amount"] == "0.95"


@pytest.mark.parametrize("wrong", ["custody", "dao", "amount", "claimed", "heap"])
def test_historical_slash_conservation_rejects_nominal_or_index_only_success(wrong):
    before, after, record = slash_states()
    if wrong == "custody":
        after["financial"]["custody"] = "1"
    elif wrong == "dao":
        after["dao_balance"] = "3"
    elif wrong == "amount":
        record["amount"] = "1"
    elif wrong == "claimed":
        record["claimed"] = True
    else:
        after["financial"]["heap_size"] = 0
    with pytest.raises(Exception):
        checks.validate_pending_slash(before, after, record)


def test_evidence_adapter_uses_only_owned_homes_and_supports_three_nodes(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    class Protocol:
        pass

    monkeypatch.setattr(
        checks, "protocol_module", lambda: SimpleNamespace(ProtocolSafetyRunner=Protocol)
    )
    nodes = []
    for index in range(3):
        home = tmp_path / str(index)
        (home / "config").mkdir(parents=True)
        (home / "config/priv_validator_key.json").write_text(json.dumps({"owned_fixture": index}))
        nodes.append({"home": home})
    live = {i: object() for i in range(3)}
    fixture = SimpleNamespace(nodes=nodes, running=live, genesis={"chain_id": "local"})
    adapter = checks.evidence_adapter(fixture)
    assert adapter.nodes == list(live.values())
    assert adapter.load_priv_validator_key(2) == {"owned_fixture": 2}


def test_real_protected_evidence_flow_uses_post_probe_baseline_and_waits_delayed_update(
    monkeypatch,
):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    state = evidence_state()
    before, after, record = slash_states()
    nodes = [{"address": letter * 40} for letter in "ABC"]
    live = {i: SimpleNamespace(moniker=str(i)) for i in range(3)}
    fixture = SimpleNamespace(
        session=object(),
        founder=SimpleNamespace(public_key="a" * 64),
        genesis_nanos=0,
        low={"index": 1},
        nodes=nodes,
        running=live,
    )
    events = []

    async def probe(*args):
        events.append("final-paid-probe")
        return state, {"height": 8, "tx_hash": "public"}

    async def checkpoint(*args):
        events.append("financial-baseline")
        return before if events.count("financial-baseline") == 1 else after

    async def query(session, node, path):
        if path.endswith("duplicate_vote_slash_bps"):
            return 500
        if path.endswith("slash_destination"):
            return "dao"
        if path.endswith("duplicate_vote_jail"):
            return True
        if path.startswith("/validators_pending_unbonds_page/"):
            return {"unbonds": [record], "next_cursor": None}
        return {"jailed": True, "pending_slash_count": 0}

    async def rpc(session, node, method, **kwargs):
        if method == "consensus_params":
            return {"consensus_params": {"evidence": state["evidence"]}}
        if method == "block":
            return {"block": {"header": headers_fixture()[1]}}
        assert kwargs["height"] == 13
        return {"validators": [{"address": n["address"], "voting_power": "10"} for n in nodes[1:]]}

    async def evidence(*args, **kwargs):
        events.append("broadcast-evidence")
        assert (
            events.index("final-paid-probe")
            < events.index("financial-baseline")
            < events.index("broadcast-evidence")
        )
        assert kwargs == {"reporter_node_index": 1, "target_node_index": 0, "evidence_height": 1}
        return {"response": {"result": {"hash": "valid-evidence"}}}

    monkeypatch.setattr(checks, "final_progress_probe", probe)
    monkeypatch.setattr(checks, "slash_checkpoint", checkpoint)
    monkeypatch.setattr(checks, "query", query)
    monkeypatch.setattr(checks, "rpc", rpc)
    monkeypatch.setattr(
        checks,
        "evidence_adapter",
        lambda fixture: SimpleNamespace(broadcast_duplicate_vote_evidence=evidence),
    )
    monkeypatch.setattr(checks, "find_evidence_block", AsyncMock(return_value=10))
    agreement = AsyncMock(return_value={"height": 13, "app_hash": "agreed"})
    monkeypatch.setattr(checks, "three_node_agreement", agreement)
    monkeypatch.setattr(checks, "anchored_record", AsyncMock(return_value=record))
    result = asyncio.run(checks.coupled_evidence_rehearsal(fixture, live[1]))
    assert result["included_height"] == 10 and result["jailed"] is True
    assert result["financial"]["slashed"] == "0.05"
    assert agreement.await_args.kwargs == {"height": 13, "results_height": 10}


def test_final_progress_probe_finishes_before_the_stake_and_dao_baseline(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    fixture = SimpleNamespace(
        session=object(), genesis={"chain_id": "local"}, founder=checks.Wallet()
    )
    monkeypatch.setattr(checks, "wait_height", AsyncMock())
    broadcast = AsyncMock()
    monkeypatch.setattr(checks, "broadcast", broadcast)
    monkeypatch.setattr(
        checks, "transaction_receipt", AsyncMock(return_value=receipt_record(height=8))
    )
    state = {"height": 9}
    monkeypatch.setattr(checks, "retention_sample", AsyncMock(return_value=state))
    got, receipt = asyncio.run(checks.final_progress_probe(fixture, object()))
    assert got == state and receipt["height"] == 8
    transaction = broadcast.await_args.args[2]
    payload = __import__("json").loads(transaction.json_bytes)["payload"]
    assert payload["nonce"] == 4 and payload["kwargs"]["amount"] == 0


def test_private_corpus_uses_the_jailed_owned_donor_and_exports_only_public_digests(
    tmp_path, monkeypatch
):
    import asyncio
    import hashlib
    import json
    from types import SimpleNamespace

    import localnet_replay_corpus

    live = {i: SimpleNamespace(abci_container=f"owned-{i}", moniker=str(i)) for i in range(3)}
    homes = [{"home": tmp_path / str(i)} for i in range(3)]
    fixture = SimpleNamespace(
        nodes=homes,
        running=live,
        output=tmp_path,
        genesis={"chain_id": "fixture"},
        session=object(),
    )
    manifest = {"height": "14", "app_hash": "abc", "chain_id": "fixture"}

    async def export(runner, session, *, node, home):
        assert runner.nodes == list(live.values()) and session is fixture.session
        assert node is live[0] and home == homes[0]["home"]
        assert runner.network == {"chain_id": "fixture"}
        assert runner.output_dir == tmp_path / "private"
        corpus = runner.output_dir / "replay-corpus"
        corpus.mkdir(parents=True)
        (corpus / "blocks.tar.gz").write_bytes(b"private raw archive")
        (corpus / "manifest.json").write_text(json.dumps(manifest))
        return corpus

    monkeypatch.setattr(localnet_replay_corpus, "export_corpus", export)
    proof = asyncio.run(checks.capture_preflight_corpus(fixture))
    assert proof == {
        "chain_id": "fixture",
        "height": 14,
        "app_hash": "abc",
        "archive_sha256": hashlib.sha256(b"private raw archive").hexdigest(),
        "manifest_sha256": hashlib.sha256(json.dumps(manifest).encode()).hexdigest(),
        "replay_performed": False,
    }
    corpus = tmp_path / "private/replay-corpus"
    assert corpus.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in corpus.iterdir())
    assert str(tmp_path) not in json.dumps(proof) and "private raw" not in json.dumps(proof)


def test_corpus_donor_stop_and_restart_reuse_the_owned_container_and_refresh_rpc(
    tmp_path, monkeypatch
):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    donor = SimpleNamespace(abci_container="owned-0", moniker="0", rpc_url="old")
    fixture = SimpleNamespace(
        running={0: donor, 1: object(), 2: object()},
        output=tmp_path,
        genesis={"chain_id": "fixture"},
    )
    stop = AsyncMock()
    command = AsyncMock()
    address = AsyncMock(return_value=SimpleNamespace(rpc_url="new"))
    rpc = AsyncMock(return_value={"ready": True})
    monkeypatch.setattr(checks, "stop_backup_source", stop)
    monkeypatch.setattr(checks, "command", command)
    monkeypatch.setattr(checks, "observer_address", address)
    monkeypatch.setattr(checks, "rpc", rpc)
    adapter = checks.PreflightReplayRunner(fixture)
    asyncio.run(adapter.stop_node_runtime(donor))
    stop.assert_awaited_once_with("owned-0")
    asyncio.run(adapter.start_node_runtime("session", donor))
    command.assert_awaited_once_with("docker", "start", "owned-0")
    address.assert_awaited_once_with("owned-0", "0")
    rpc.assert_awaited_once_with("session", donor, "status")
    assert donor.rpc_url == "new" and adapter.nodes[0] is donor


def test_slash_checkpoint_reads_exact_principal_and_dao_after_the_final_probe(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    financial = AsyncMock(return_value=finances(locked=True))
    query = AsyncMock(return_value={"__fixed__": "123.00000000000000000001"})
    monkeypatch.setattr(checks, "financial_state", financial)
    monkeypatch.setattr(checks, "query", query)
    result = asyncio.run(checks.slash_checkpoint("session", "node", "owner"))
    assert result == {
        "financial": finances(locked=True),
        "dao_balance": "123.00000000000000000001",
    }
    financial.assert_awaited_once_with("session", "node", "owner")
    query.assert_awaited_once_with("session", "node", "/get/currency.balances:dao")


@pytest.mark.parametrize("boundary", ["nominal-block", "nominal-time"])
def test_expired_nominal_evidence_requires_strictly_exceeding_both_windows(boundary):
    state = evidence_state()
    if boundary == "nominal-block":
        state["height"] = 6
    else:
        state["last_block_nanos"] = int(state["evidence"]["max_age_duration"])
    with pytest.raises(Exception, match="has not exceeded"):
        checks.validate_coupled_evidence_ages(state, 0)


def test_coupled_window_includes_evidence_exactly_at_its_protected_block_boundary():
    state = evidence_state()
    state["evidence"]["max_age_num_blocks"] = str(state["height"] - 1)
    proof = checks.validate_coupled_evidence_ages(state, 0)
    assert proof["age_blocks"] == int(proof["effective_evidence"]["max_age_num_blocks"])


def raw_round_state_dump(low_prevote):
    consensus, native, _, _, low = initial_fixture()
    prevotes = consensus["round_state"]["height_vote_set"][0]["prevotes"]
    prevotes[1] = low_prevote
    dump = {
        "round_state": {
            "height": "1",
            "round": 0,
            "step": 4,
            "validators": native,
            "votes": [{"round": 0, "prevotes": prevotes}],
        }
    }
    return dump, low


def test_round0_prevote_wait_holds_until_lows_own_prevote_is_recorded():
    # CometBFT enters the prevote step before its queued own vote is added.
    pending, low = raw_round_state_dump("nil-Vote")
    assert checks.low_round0_prevote_recorded(pending, low) is False

    recorded, low = raw_round_state_dump(
        initial_fixture()[0]["round_state"]["height_vote_set"][0]["prevotes"][1]
    )
    assert checks.low_round0_prevote_recorded(recorded, low) is True
    # A LOW outside the validator set has no prevote of its own to wait for.
    assert checks.low_round0_prevote_recorded(recorded, {**low, "address": "F" * 40}) is False

    recorded["round_state"]["step"] = 3
    assert checks.low_round0_prevote_recorded(recorded, low) is False
