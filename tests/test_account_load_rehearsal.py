import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "account_load", Path(__file__).resolve().parents[1] / "scripts/account-load-rehearsal.py"
)
load = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(load)


def tx(nonce=0, hash="a", sender="sender"):
    return dict(nonce=nonce, hash=hash, sender=sender, amount=1, to="receiver")


def test_balance_oracle_conserves_funds_across_an_explicit_recovery_nonce():
    initial = {"sender": 10, "receiver": 0}
    assert load.expected_balances(
        initial, [tx(), tx(2, "b")], "receiver", [{"address": "sender", "nonce": 1}]
    ) == {"sender": 8, "receiver": 2}
    assert initial == {"sender": 10, "receiver": 0}


@pytest.mark.parametrize(
    "receipts",
    [[tx(), tx()], [tx(), tx(2, "b")], [tx(), tx(0, "b")], [{**tx(), "amount": 2}], [tx(1)]],
)
def test_oracle_rejects_duplicate_receipts_nonce_gaps_and_unexpected_amounts(receipts):
    with pytest.raises(AssertionError):
        load.expected_balances({"sender": 10, "receiver": 0}, receipts, "receiver")


def test_explicit_transition_cannot_hide_other_nonce_gaps():
    with pytest.raises(AssertionError):
        load.expected_balances(
            {"sender": 10, "receiver": 0},
            [tx(), tx(3, "b")],
            "receiver",
            [{"address": "sender", "nonce": 1}],
        )
