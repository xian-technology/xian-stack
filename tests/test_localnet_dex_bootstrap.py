from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import ModuleType


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
