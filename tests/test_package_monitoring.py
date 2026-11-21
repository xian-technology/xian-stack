from __future__ import annotations

import hashlib
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).absolute().parents[1]


def test_monitoring_release_bundle_is_reproducible_and_contains_canonical_assets(tmp_path):
    first = tmp_path / "nested/first"
    second = tmp_path / "second"
    second.mkdir()
    for output in (first, second):
        subprocess.run([sys.executable, ROOT / "scripts/package_monitoring.py", output], check=True)

    archive = first / "xian-monitoring.tar"
    assert archive.read_bytes() == (second / "xian-monitoring.tar").read_bytes()
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert archive.with_suffix(".tar.sha256").read_text() == f"{checksum}  {archive.name}\n"

    expected = {
        "alertmanager/alertmanager.example.yml",
        "grafana/dashboards/xian-bds-recovery.json",
        "grafana/dashboards/xian-overview.json",
        "grafana/dashboards/xian-shared-network.json",
        "grafana/dashboards/xian-vm-runtime.json",
        "grafana/provisioning/dashboards/xian.yml",
        "grafana/provisioning/datasources/xian.yml",
        "prometheus/rules/block-cadence.yml",
        "prometheus/rules/shared-network.yml",
    }
    with tarfile.open(archive) as bundle:
        assert bundle.getnames() == sorted(expected)
        for member in bundle.getmembers():
            assert member.isfile()
            assert (member.uid, member.gid, member.mtime, member.mode) == (0, 0, 0, 0o644)
            assert (
                bundle.extractfile(member).read()
                == (ROOT / "monitoring" / member.name).read_bytes()
            )
