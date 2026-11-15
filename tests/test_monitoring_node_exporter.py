"""node-exporter feeds the disk alerts: scraped by every Prometheus config, never published."""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).absolute().parents[1]


def service_block(compose: str, name: str) -> str:
    """The lines of one top-level compose service (two-space indented key)."""
    lines = compose.splitlines()
    start = lines.index(f"  {name}:")
    block = [lines[start]]
    for line in lines[start + 1 :]:
        if line.strip() and not line.startswith("    "):
            break
        block.append(line)
    return "\n".join(block)


@pytest.mark.parametrize("config", ["integrated.yml", "fidelity.yml"])
def test_prometheus_scrapes_node_exporter(config):
    source = (ROOT / "monitoring/prometheus" / config).read_text(encoding="utf-8")

    assert "  - job_name: xian_node\n" in source
    assert "          - node-exporter:9100\n" in source


def test_node_exporter_reads_the_host_read_only_and_publishes_no_port():
    compose = (ROOT / "docker-compose-monitoring.yml").read_text(encoding="utf-8")
    block = service_block(compose, "node-exporter")

    assert "image: ${XIAN_NODE_EXPORTER_IMAGE}" in block
    assert '"--path.rootfs=/host"' in block
    assert "- /:/host:ro\n" in block + "\n"
    assert "ports:" not in block
    assert "- xian-net" in block


def test_monitoring_targets_start_and_stop_node_exporter():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    recipes = [
        line
        for line in makefile.splitlines()
        if line.startswith("\t") and " prometheus grafana" in line
    ]

    assert len(recipes) == 6
    assert all("prometheus grafana node-exporter" in line for line in recipes)
