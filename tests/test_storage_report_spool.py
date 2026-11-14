"""The storage report finds the BDS spool where the node's xian.toml puts it."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).absolute().parents[1]
SPEC = importlib.util.spec_from_file_location("storage_report", ROOT / "scripts/storage-report.py")
report = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(report)


@pytest.mark.parametrize(
    ("bds_table", "expected"),
    [
        (None, "xian/bds-spool"),
        ('spool_dir = ""', "xian/bds-spool"),
        ('spool_dir = "/root/.cometbft/spool-elsewhere"', "spool-elsewhere"),
    ],
)
def test_spool_follows_the_node_configuration(tmp_path, bds_table, expected):
    config = tmp_path / "config"
    config.mkdir()
    if bds_table is not None:
        (config / "xian.toml").write_text(f"bds_enabled = true\n\n[bds]\n{bds_table}\n")

    assert report.bds_spool_dir(tmp_path) == tmp_path / expected


def test_spool_outside_the_node_home_is_reported_as_configured(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "xian.toml").write_text('[bds]\nspool_dir = "/srv/spool"\n')

    assert report.bds_spool_dir(tmp_path) == Path("/srv/spool")
