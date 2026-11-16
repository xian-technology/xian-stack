from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


def _load_bootstrap_module() -> ModuleType:
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "localnet-dex-bootstrap.py"
    spec = importlib.util.spec_from_file_location(
        "localnet_dex_bootstrap",
        script_path,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bootstrap = _load_bootstrap_module()


def test_coerce_number_converts_floats_through_strings() -> None:
    assert bootstrap.coerce_number(0.1) == Decimal("0.1")
    assert bootstrap.coerce_number(1) == Decimal("1")
    assert bootstrap.coerce_number("0.0001") == Decimal("0.0001")


def test_parse_args_keeps_dex_demo_amounts_as_decimal(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "localnet-dex-bootstrap.py",
            "--demo-token-supply",
            "123.45",
            "--liquidity-currency-amount",
            "10.5",
            "--liquidity-demo-token-amount",
            "20.25",
            "--test-swap-amount",
            "0.3",
        ],
    )

    args = bootstrap.parse_args()

    assert args.demo_token_supply == Decimal("123.45")
    assert args.liquidity_currency_amount == Decimal("10.5")
    assert args.liquidity_demo_token_amount == Decimal("20.25")
    assert args.test_swap_amount == Decimal("0.3")


@pytest.mark.parametrize(
    "removed_option",
    [
        # Fixed budgets pay half of every unused chi; the bootstrap always
        # simulates instead.
        ["--chi-budget-mode", "fixed"],
        # A key on the command line is visible to every local user (ps) and
        # lands in shell history.
        ["--deployer-private-key", "ab" * 32],
    ],
)
def test_parse_args_refuses_removed_options(monkeypatch, removed_option) -> None:
    monkeypatch.setattr(sys, "argv", ["localnet-dex-bootstrap.py", *removed_option])

    with pytest.raises(SystemExit):
        bootstrap.parse_args()


def _key_args(**overrides) -> SimpleNamespace:
    values = {
        "deployer_key_file": None,
        "rpc_url": None,
        "validator_key_path": Path("/nonexistent/priv_validator_key.json"),
    }
    return SimpleNamespace(**{**values, **overrides})


def test_deployer_key_comes_from_the_key_file_before_the_environment(monkeypatch, tmp_path) -> None:
    key_file = tmp_path / "deployer.key"
    key_file.write_text(f"  {'cd' * 32}\n", encoding="utf-8")
    monkeypatch.setenv("XIAN_DEX_DEPLOYER_PRIVATE_KEY", "ef" * 32)

    resolved = bootstrap.resolve_deployer_private_key(
        args=_key_args(deployer_key_file=key_file), network=None
    )

    assert resolved == "cd" * 32


def test_deployer_key_comes_from_the_environment_without_a_key_file(monkeypatch) -> None:
    monkeypatch.setenv("XIAN_DEX_DEPLOYER_PRIVATE_KEY", "ef" * 32)

    assert bootstrap.resolve_deployer_private_key(args=_key_args(), network=None) == "ef" * 32


def test_an_empty_key_file_is_refused(tmp_path) -> None:
    key_file = tmp_path / "deployer.key"
    key_file.write_text("\n", encoding="utf-8")

    with pytest.raises(bootstrap.DexBootstrapError, match="empty"):
        bootstrap.resolve_deployer_private_key(
            args=_key_args(deployer_key_file=key_file), network=None
        )


def test_parse_args_reads_the_key_file_path_from_the_environment(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XIAN_DEX_DEPLOYER_KEY_FILE", str(tmp_path / "deployer.key"))
    monkeypatch.setattr(sys, "argv", ["localnet-dex-bootstrap.py"])

    assert bootstrap.parse_args().deployer_key_file == tmp_path / "deployer.key"


def test_deadline_value_uses_xian_vm_time_payload(monkeypatch) -> None:
    class FixedDateTime:
        @classmethod
        def now(cls, tz):
            assert tz is UTC
            return datetime(2026, 5, 21, 12, 0, 30, 123456, tzinfo=UTC)

    monkeypatch.setattr(bootstrap, "datetime", FixedDateTime)

    assert bootstrap.deadline_value(seconds_from_now=90) == {
        "__time__": [2026, 5, 21, 12, 2, 0, 123456]
    }


def test_require_success_accepts_submission_without_receipt() -> None:
    bootstrap.require_success(
        "deploy con_pairs",
        SimpleNamespace(
            submitted=True,
            accepted=True,
            message=None,
            receipt=None,
        ),
    )


def test_every_submission_lets_xian_py_simulate_its_chi() -> None:
    calls: list[tuple[str, dict]] = []

    class FakeClient:
        contract_deployed = False

        def get_contract_source(self, name):
            assert name == "con_test"
            return "code" if self.contract_deployed else None

        def submit_contract(self, name, code, *, args, **options):
            assert (name, code, args) == ("con_test", "code", None)
            calls.append(("deploy", options))
            self.contract_deployed = True
            return _successful_submission()

        def send_tx(self, contract, function, kwargs, **options):
            assert (contract, function, kwargs) == ("con_test", "run", {"value": 1})
            calls.append(("call", options))
            return _successful_submission()

    def _successful_submission():
        return SimpleNamespace(
            submitted=True,
            accepted=True,
            message=None,
            tx_hash="abc",
            nonce=1,
            chi_supplied=44,
            chi_estimated=42,
            receipt=SimpleNamespace(success=True, message=None),
        )

    client = FakeClient()
    bootstrap.submit_contract_if_missing(
        client,
        name="con_test",
        code="code",
        constructor_args=None,
        mode="checktx",
        receipt_timeout_seconds=1,
    )
    bootstrap.send_call(
        client,
        label="Calling con_test...",
        contract="con_test",
        function="run",
        kwargs={"value": 1},
        mode="checktx",
        receipt_timeout_seconds=1,
    )

    assert [kind for kind, _ in calls] == ["deploy", "call"]
    assert all(options.get("chi") is None for _, options in calls)


def test_seed_demo_pool_registers_lp_before_auto_creating_pair(monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    pair_reads = 0

    class FakeClient:
        wallet = SimpleNamespace(public_key="deployer")

        def get_state(self, contract, variable, *args):
            nonlocal pair_reads
            assert contract == "con_pairs"
            if variable == "toks_to_pair":
                pair_reads += 1
                return None if pair_reads == 1 else 1
            if variable == "registered_lp_tokens":
                return None
            raise AssertionError(f"unexpected state read: {variable} {args}")

    def fake_send_call(
        client,
        *,
        label,
        contract,
        function,
        kwargs,
        mode,
        receipt_timeout_seconds,
    ):
        calls.append(
            {
                "label": label,
                "contract": contract,
                "function": function,
                "kwargs": kwargs,
            }
        )
        return calls[-1]

    monkeypatch.setattr(bootstrap, "send_call", fake_send_call)
    monkeypatch.setattr(
        bootstrap,
        "get_pair_snapshot",
        lambda client, pair_id: {"pair_id": pair_id},
    )

    result = bootstrap.seed_demo_pool(
        FakeClient(),
        token_contract="con_dex_demo_token",
        lp_contract="con_dex_demo_lp",
        liquidity_currency_amount=Decimal("10"),
        liquidity_demo_token_amount=Decimal("20"),
        top_up_liquidity=False,
        mode="sync",
        receipt_timeout_seconds=1,
    )

    assert [call["function"] for call in calls] == [
        "registerLpToken",
        "approve",
        "approve",
        "addLiquidity",
    ]
    assert calls[0]["kwargs"] == {
        "tokenA": "con_dex_demo_token",
        "tokenB": "currency",
        "lpToken": "con_dex_demo_lp",
    }
    assert result["action"] == "seeded"


def _bootstrap_with_fakes(monkeypatch, argv: list[str]) -> tuple[dict, list[tuple]]:
    """Run bootstrap() against a fake chain; return its report and the steps it took."""
    steps: list[tuple] = []

    class FakeXian:
        chain_id = "xian-fake-1"

        def __init__(self, rpc_url, *, chain_id, wallet, config):
            steps.append(("connect", rpc_url, chain_id))
            self.wallet = wallet

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def refresh_nonce(self):
            pass

    def fake_submit(client, *, name, code, constructor_args, mode, receipt_timeout_seconds):
        steps.append(("deploy", name, code, constructor_args))
        return {"contract": name, "action": "deployed"}

    monkeypatch.setattr(bootstrap, "Xian", FakeXian)
    monkeypatch.setattr(bootstrap, "wait_for_rpc_ready", lambda url, timeout_seconds: None)
    monkeypatch.setattr(bootstrap, "load_network", lambda path: None)
    monkeypatch.setattr(
        bootstrap,
        "load_dex_contract_sources",
        lambda args: {
            "kind": "directory",
            "bundle_path": None,
            "contracts_dir": Path("/dex"),
            "core_sources": {"con_pairs": "pairs", "con_dex": "dex"},
            "lp_token_source": "lp",
            "demo_token_source": "token",
        },
    )
    monkeypatch.setattr(bootstrap, "submit_contract_if_missing", fake_submit)
    monkeypatch.setattr(
        bootstrap,
        "seed_demo_pool",
        lambda client, **kw: steps.append(("seed", kw["token_contract"])) or "pool",
    )
    monkeypatch.setattr(
        bootstrap,
        "emit_test_swap",
        lambda client, **kw: steps.append(("swap", kw["amount"])) or "swap",
    )
    monkeypatch.setattr(bootstrap, "get_pair_id", lambda client, a, b: 3)
    monkeypatch.setattr(
        bootstrap, "get_pair_snapshot", lambda client, pair_id: {"pair_id": pair_id}
    )
    monkeypatch.setenv("XIAN_DEX_DEPLOYER_PRIVATE_KEY", "ab" * 32)
    monkeypatch.setattr(
        sys, "argv", ["localnet-dex-bootstrap.py", "--rpc-url", "http://node:26657", *argv]
    )
    report = bootstrap.bootstrap(bootstrap.parse_args())
    return report, steps


def test_bootstrap_deploys_core_then_demo_contracts_seeds_and_swaps(monkeypatch) -> None:
    report, steps = _bootstrap_with_fakes(
        monkeypatch, ["--seed-demo-pool", "--emit-test-swap", "--chain-id", "xian-x"]
    )

    deployer = report["deployer"]
    assert steps == [
        ("connect", "http://node:26657", "xian-x"),
        ("deploy", "con_pairs", "pairs", None),
        ("deploy", "con_dex", "dex", None),
        (
            "deploy",
            "con_dex_demo_token",
            "token",
            {
                "owner": deployer,
                "supply": Decimal("1000000"),
                "token_name": "Xian DEX Demo Token",
                "token_symbol": "XDT",
                "precision": 8,
            },
        ),
        (
            "deploy",
            "con_dex_demo_lp",
            "lp",
            {
                "token_name": "XDT/XIAN LP",
                "token_symbol": "XIAN-XDT-LP",
                "operator_address": deployer,
                "minter_address": "con_pairs",
            },
        ),
        ("seed", "con_dex_demo_token"),
        ("swap", Decimal("10")),
    ]
    assert report == {
        "ok": True,
        "chain_id": "xian-fake-1",
        "rpc_url": "http://node:26657",
        "deployer": deployer,
        "execution_mode": "xian_vm_v2",
        "dex_source": "directory",
        "dex_bundle": None,
        "dex_contracts_dir": "/dex",
        "contracts": {
            "router": "con_dex",
            "pairs": "con_pairs",
            "helper": "con_dex_helper",
            "demo_token": "con_dex_demo_token",
            "demo_lp_token": "con_dex_demo_lp",
        },
        "deployments": [
            {"contract": name, "action": "deployed"}
            for name in ("con_pairs", "con_dex", "con_dex_demo_token", "con_dex_demo_lp")
        ],
        "demo_pool": "pool",
        "test_swap": "swap",
        "pair": {"pair_id": 3},
    }


def test_bootstrap_without_a_demo_pool_deploys_only_the_core(monkeypatch) -> None:
    report, steps = _bootstrap_with_fakes(
        monkeypatch, ["--no-seed-demo-pool", "--no-emit-test-swap", "--no-deploy-helper"]
    )

    assert [step[1] for step in steps if step[0] == "deploy"] == ["con_pairs", "con_dex"]
    assert not [step for step in steps if step[0] in ("seed", "swap")]
    assert report["contracts"] == {
        "router": "con_dex",
        "pairs": "con_pairs",
        "helper": None,
        "demo_token": None,
        "demo_lp_token": None,
    }
    assert (report["demo_pool"], report["test_swap"]) == (None, None)


def test_bootstrap_needs_an_rpc_url_without_localnet_metadata(monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "load_network", lambda path: None)
    monkeypatch.setattr(sys, "argv", ["localnet-dex-bootstrap.py"])

    with pytest.raises(bootstrap.DexBootstrapError, match="pass --rpc-url"):
        bootstrap.bootstrap(bootstrap.parse_args())


def _seed(monkeypatch, *, pair_id, snapshot=None, registered_lp=None, top_up=False):
    sent: list[str] = []
    # The pair exists once liquidity was added (addLiquidity creates it).
    monkeypatch.setattr(
        bootstrap, "get_pair_id", lambda client, a, b: 1 if "addLiquidity" in sent else pair_id
    )
    monkeypatch.setattr(
        bootstrap, "get_pair_snapshot", lambda client, pid: snapshot or {"pair_id": pid}
    )
    monkeypatch.setattr(bootstrap, "get_registered_lp_token", lambda client, a, b: registered_lp)
    monkeypatch.setattr(
        bootstrap, "send_call", lambda client, **kw: sent.append(kw["function"]) or kw["function"]
    )
    monkeypatch.setattr(
        bootstrap,
        "approve_for_dex",
        lambda client, **kw: sent.append(f"approve {kw['token']}") or "approve",
    )
    result = bootstrap.seed_demo_pool(
        SimpleNamespace(wallet=SimpleNamespace(public_key="deployer")),
        token_contract="con_dex_demo_token",
        lp_contract="con_dex_demo_lp",
        liquidity_currency_amount=Decimal("10"),
        liquidity_demo_token_amount=Decimal("20"),
        top_up_liquidity=top_up,
        mode="sync",
        receipt_timeout_seconds=1,
    )
    return result, sent


def test_seed_demo_pool_skips_a_pair_that_already_has_liquidity(monkeypatch) -> None:
    snapshot = {"reserve0": "5", "reserve1": "7", "lp_token": "con_dex_demo_lp"}
    result, sent = _seed(monkeypatch, pair_id=1, snapshot=snapshot)

    assert result == {
        "action": "skipped",
        "reason": "pair_already_seeded",
        "pair": snapshot,
        "operations": [],
    }
    assert sent == []


def test_seed_demo_pool_tops_up_an_existing_pair_without_registering(monkeypatch) -> None:
    snapshot = {"reserve0": "5", "reserve1": "7", "lp_token": "con_dex_demo_lp"}
    _, sent = _seed(monkeypatch, pair_id=1, snapshot=snapshot, top_up=True)

    assert sent == ["approve currency", "approve con_dex_demo_token", "addLiquidity"]


def test_seed_demo_pool_refuses_an_existing_pair_with_another_lp_token(monkeypatch) -> None:
    snapshot = {"reserve0": "0", "reserve1": "0", "lp_token": "con_other_lp"}
    with pytest.raises(bootstrap.DexBootstrapError, match="existing demo pair uses LP token"):
        _seed(monkeypatch, pair_id=1, snapshot=snapshot)


def test_seed_demo_pool_refuses_another_registered_lp_token(monkeypatch) -> None:
    with pytest.raises(
        bootstrap.DexBootstrapError, match="registered demo LP token is 'con_other_lp'"
    ):
        _seed(monkeypatch, pair_id=None, registered_lp="con_other_lp")


def test_seed_demo_pool_does_not_register_an_lp_token_twice(monkeypatch) -> None:
    _, sent = _seed(monkeypatch, pair_id=None, registered_lp="con_dex_demo_lp")

    assert sent[:2] == ["approve currency", "approve con_dex_demo_token"]
    assert "registerLpToken" not in sent


def _validator_key_file(tmp_path, seed: bytes) -> Path:
    import base64
    import json

    path = tmp_path / "priv_validator_key.json"
    path.write_text(
        json.dumps({"priv_key": {"value": base64.b64encode(seed + b"\x00" * 32).decode()}})
    )
    return path


def test_a_single_node_rpc_uses_its_validator_key_before_the_localnet_founder(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("XIAN_DEX_DEPLOYER_PRIVATE_KEY", raising=False)
    args = _key_args(
        rpc_url="http://node:26657", validator_key_path=_validator_key_file(tmp_path, b"\x11" * 32)
    )

    assert (
        bootstrap.resolve_deployer_private_key(args=args, network={"founder_key": "ff" * 32})
        == "11" * 32
    )


def test_the_localnet_founder_key_is_used_without_an_rpc_url(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("XIAN_DEX_DEPLOYER_PRIVATE_KEY", raising=False)
    args = _key_args(validator_key_path=_validator_key_file(tmp_path, b"\x11" * 32))

    assert (
        bootstrap.resolve_deployer_private_key(args=args, network={"founder_key": "ff" * 32})
        == "ff" * 32
    )


def test_the_validator_key_is_the_last_resort_and_its_absence_is_refused(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("XIAN_DEX_DEPLOYER_PRIVATE_KEY", raising=False)
    found = _key_args(validator_key_path=_validator_key_file(tmp_path, b"\x22" * 32))
    assert bootstrap.resolve_deployer_private_key(args=found, network=None) == "22" * 32

    with pytest.raises(bootstrap.DexBootstrapError, match="pass --deployer-key-file"):
        bootstrap.resolve_deployer_private_key(args=_key_args(), network=None)
