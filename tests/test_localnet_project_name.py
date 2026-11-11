"""The generated localnet is its own Compose project, apart from the single node."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("project_localnet", ROOT / "scripts/localnet-init.py")
localnet = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(localnet)


def write_localnet(directory: Path) -> Path:
    with patch.object(localnet, "STACK_DIR", directory):
        localnet.write_compose_file(
            [{"index": 0, "moniker": "node-0"}],
            "integrated",
            bds_enabled=False,
            bds_node_index=0,
            profiling_enabled=False,
            profiling_recent_blocks=20,
        )
    return directory / "docker-compose-localnet.yml"


def test_generated_localnet_names_its_own_compose_project(tmp_path):
    compose = json.loads(write_localnet(tmp_path).read_text())

    # node-stop/abci-down use `down --remove-orphans` in the stack directory's
    # project; a separately named localnet is never one of its orphans.
    assert compose["name"] == "xian-localnet"
    assert compose["name"] != ROOT.name


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI unavailable")
def test_compose_resolves_the_localnet_project_name_inside_the_stack_directory(tmp_path):
    stack_dir = tmp_path / "xian-stack"
    stack_dir.mkdir()
    rendered = subprocess.run(
        ["docker", "compose", "-f", str(write_localnet(stack_dir)), "config", "--format", "json"],
        cwd=stack_dir,
        env={key: value for key, value in os.environ.items() if key != "COMPOSE_PROJECT_NAME"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(rendered.stdout)["name"] == "xian-localnet"
