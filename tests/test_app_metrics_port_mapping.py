"""The app-metrics listener stays on the compose container port; only the host side moves."""

from __future__ import annotations

import re
import sys
from pathlib import Path

STACK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STACK_ROOT / "scripts"))

import configure_node  # noqa: E402

PUBLISHED_APP_METRICS = re.compile(
    r"- target: (\d+)\n\s+published: \"\$\{XIAN_APP_METRICS_PORT\}\"",
)


def _environment(**overrides: str) -> dict[str, str]:
    environment = {key: "value" for key, _option in configure_node.VALUE_OPTIONS}
    environment.update({key: "1" for key, *_flags in configure_node.BOOLEAN_OPTIONS})
    environment["XIAN_BDS_PASSWORD"] = "placeholder"
    environment.update(overrides)
    return environment


def _option(arguments: list[str], name: str) -> str:
    return arguments[arguments.index(name) + 1]


def test_published_metrics_port_is_not_the_listen_port() -> None:
    arguments = configure_node.configuration_arguments(_environment(XIAN_APP_METRICS_PORT="39108"))

    assert arguments.count("--metrics-port") == 1
    assert _option(arguments, "--metrics-port") == configure_node.APP_METRICS_CONTAINER_PORT
    assert "39108" not in arguments


def test_node_listens_on_every_container_interface() -> None:
    arguments = configure_node.configuration_arguments(
        _environment(XIAN_APP_METRICS_LISTEN_HOST="127.0.0.1", XIAN_APP_METRICS_HOST="10.0.0.5")
    )

    assert arguments.count("--metrics-host") == 1
    assert _option(arguments, "--metrics-host") == configure_node.APP_METRICS_CONTAINER_HOST
    assert configure_node.APP_METRICS_CONTAINER_HOST == "0.0.0.0"


def test_compose_binds_the_published_port_to_the_host_address() -> None:
    compose = (STACK_ROOT / "docker-compose-abci.yml").read_text(encoding="utf-8")
    published = re.findall(
        r"published: \"\$\{XIAN_APP_METRICS_PORT\}\"\n\s+host_ip: \"([^\"]+)\"", compose
    )

    assert published and set(published) == {"${XIAN_APP_METRICS_HOST}"}


def test_compose_publishes_the_port_the_node_listens_on() -> None:
    compose = (STACK_ROOT / "docker-compose-abci.yml").read_text(encoding="utf-8")
    targets = PUBLISHED_APP_METRICS.findall(compose)

    assert targets, "compose no longer publishes XIAN_APP_METRICS_PORT"
    assert set(targets) == {configure_node.APP_METRICS_CONTAINER_PORT}
