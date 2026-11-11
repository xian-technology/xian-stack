"""Relayer environment numbers are finite before submission config is built."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from shielded_relayer_service import load_config_from_env


@pytest.mark.parametrize(
    "name",
    [
        "XIAN_SHIELDED_RELAYER_TIMEOUT_SECONDS",
        "XIAN_SHIELDED_RELAYER_POLL_INTERVAL_SECONDS",
        "XIAN_SHIELDED_RELAYER_CHI_MARGIN",
    ],
)
@pytest.mark.parametrize("value", ["NaN", "inf", "-inf", "1e9999"])
def test_config_rejects_non_finite_environment_numbers(name, value):
    with patch.dict(
        "os.environ",
        {"XIAN_SHIELDED_RELAYER_PRIVATE_KEY": "1" * 64, name: value},
        clear=True,
    ):
        with pytest.raises(ValueError, match=f"{name} must be a finite number"):
            load_config_from_env()


@pytest.mark.parametrize("value", ["0", "0.25", "1e2"])
def test_config_preserves_finite_environment_numbers(value):
    with patch.dict(
        "os.environ",
        {
            "XIAN_SHIELDED_RELAYER_PRIVATE_KEY": "1" * 64,
            "XIAN_SHIELDED_RELAYER_TIMEOUT_SECONDS": value,
            "XIAN_SHIELDED_RELAYER_POLL_INTERVAL_SECONDS": value,
            "XIAN_SHIELDED_RELAYER_CHI_MARGIN": value,
        },
        clear=True,
    ):
        config = load_config_from_env()
    assert config.timeout_seconds == float(value)
    assert config.poll_interval_seconds == float(value)
    assert config.chi_margin == float(value)


@pytest.mark.parametrize("empty", [None, "", " "])
def test_config_preserves_default_environment_numbers(empty):
    environment = {"XIAN_SHIELDED_RELAYER_PRIVATE_KEY": "1" * 64}
    if empty is not None:
        environment["XIAN_SHIELDED_RELAYER_TIMEOUT_SECONDS"] = empty
        environment["XIAN_SHIELDED_RELAYER_POLL_INTERVAL_SECONDS"] = empty
        environment["XIAN_SHIELDED_RELAYER_CHI_MARGIN"] = empty
    with patch.dict("os.environ", environment, clear=True):
        config = load_config_from_env()
    assert config.timeout_seconds == 30.0
    assert config.poll_interval_seconds == 0.25
    assert config.chi_margin == 1.0
