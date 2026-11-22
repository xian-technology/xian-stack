"""Preflight must not leave generated files in the optional sibling checkout."""

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_failed_intentkit_compose_check_leaves_env_absent(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    validator = scripts / "validate-stack.sh"
    shutil.copyfile(ROOT / "scripts/validate-stack.sh", validator)
    (scripts / "stack-env.sh").write_text(
        'stack_root="${VALIDATION_ROOT}"\n'
        "export_stack_env() { :; }\n"
        "prepare_stack_dirs() { :; }\n"
        "require_stack_paths() { :; }\n"
        "require_docker() { :; }\n"
        "require_uv() { :; }\n"
    )
    (scripts / "release_manifest.py").touch()
    intentkit = tmp_path / "intentkit"
    deployment = intentkit / "deployment"
    deployment.mkdir(parents=True)
    (deployment / "docker-compose.yml").touch()
    docker = tmp_path / "docker"
    docker.write_text('#!/bin/bash\n[[ "$*" != *docker-compose-intentkit.yml* ]] || exit 23\n')
    docker.chmod(0o700)

    env = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "VALIDATION_ROOT": str(tmp_path),
        "XIAN_INTENTKIT_DIR": str(intentkit),
    }
    result = subprocess.run(["bash", str(validator)], env=env, capture_output=True, text=True)

    assert result.returncode == 23, result.stderr
    assert not (deployment / ".env").exists()
