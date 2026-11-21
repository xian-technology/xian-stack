"""Build the monitoring release artifact from the stack's canonical assets."""

from __future__ import annotations

import argparse
import hashlib
import io
import tarfile
from pathlib import Path

MONITORING_ROOT = Path(__file__).resolve().parents[1] / "monitoring"
ASSETS = (
    "alertmanager/alertmanager.example.yml",
    "grafana/dashboards/xian-bds-recovery.json",
    "grafana/dashboards/xian-overview.json",
    "grafana/dashboards/xian-shared-network.json",
    "grafana/dashboards/xian-vm-runtime.json",
    "grafana/provisioning/dashboards/xian.yml",
    "grafana/provisioning/datasources/xian.yml",
    "prometheus/rules/block-cadence.yml",
    "prometheus/rules/shared-network.yml",
)


def package_monitoring(output: Path) -> None:
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name in sorted(ASSETS):
            content = (MONITORING_ROOT / name).read_bytes()
            member = tarfile.TarInfo(name)
            member.size = len(content)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(content))

    output.mkdir(parents=True, exist_ok=True)
    bundle = output / "xian-monitoring.tar"
    bundle.write_bytes(payload.getbuffer())
    checksum = hashlib.sha256(payload.getbuffer()).hexdigest()
    bundle.with_suffix(".tar.sha256").write_text(f"{checksum}  {bundle.name}\n", encoding="ascii")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", type=Path)
    package_monitoring(parser.parse_args().output_dir)
