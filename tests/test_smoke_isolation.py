"""Both public Make smoke entrypoints must leave existing node resources alone."""

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).absolute().parents[1]
REAL_MAKE = shutil.which("make")
CREDENTIALS = '# xian-stack-credentials-v1\nXIAN_POSTGRAPHILE_PASSWORD="original-credentials"\n'


def resource(state, kind, name):
    return state / kind / hashlib.sha256(name.encode()).hexdigest()


@pytest.fixture
def smoke(tmp_path):
    stack = tmp_path / "xian-stack"
    scripts = stack / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(ROOT / "Makefile", stack)
    for pattern in ("smoke*", "stack-env.sh", "stack_credentials.py"):
        for source in (ROOT / "scripts").glob(pattern):
            if source.is_file():
                shutil.copy2(source, scripts)
    validator = scripts / "validate-stack.sh"
    validator.write_text('#!/bin/bash\nexit "${SMOKE_STUB_PREFLIGHT_EXIT:-0}"\n')
    validator.chmod(0o755)
    for repo in (
        "xian-cli",
        "xian-abci",
        "xian-configs",
        "xian-contracting",
        "xian-py",
        "xian-dex-automation",
    ):
        (tmp_path / repo).mkdir()
    state = tmp_path / "resources"
    for kind, name in (("images", "existing:local"), ("projects", "existing")):
        p = resource(state, kind, name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(name)
    existing = tmp_path / "existing-node"
    (existing / "config").mkdir(parents=True)
    (existing / "config/genesis.json").write_text("original genesis")
    (existing / "credentials").write_text(CREDENTIALS)
    (existing / "credentials").chmod(0o600)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("docker", "make", "uv", "curl"):
        p = bin_dir / tool
        p.write_text(f"#!{sys.executable}\n" + (ROOT / "tests/smoke_stub.py").read_text())
        p.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "SMOKE_STUB_STATE": str(state),
        "SMOKE_STUB_LOG": str(log),
        "SMOKE_STUB_MAKE": REAL_MAKE,
        "XIAN_COMETBFT_HOME": str(existing),
        "XIAN_BDS_DATA_DIR": str(existing),
        "XIAN_STACK_SECRETS_ENV": str(existing / "credentials"),
        "XIAN_NODE_INTEGRATED_IMAGE": "existing:local",
        "COMPOSE_PROJECT_NAME": "existing",
        "XIAN_SMOKE_ROOT": str(existing),
        "XIAN_SMOKE_CLI_ROOT": str(existing),
        "XIAN_SMOKE_STATUS_URL": "http://existing-node/status",
        "XIAN_NODE_IMAGE_MODE": "registry",
        "XIAN_SMOKE_COMETBFT_RPC_HOST": "0.0.0.0",
    }
    fixture = stack, state, existing, log, env
    yield fixture
    if log.exists():
        for call in calls(fixture):
            home = Path(call["XIAN_COMETBFT_HOME"])
            if home.parent.name.startswith("xian-smoke-"):
                shutil.rmtree(home.parent, ignore_errors=True)


def start(smoke, target, **overrides):
    stack, _, existing, _, env = smoke
    return subprocess.Popen(
        [
            REAL_MAKE,
            "--no-print-directory",
            target,
            f"XIAN_COMETBFT_HOME={existing}",
            "XIAN_NODE_INTEGRATED_IMAGE=existing:local",
            "COMPOSE_PROJECT_NAME=existing",
        ],
        cwd=stack,
        env={**env, **overrides},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


def finish(process):
    stdout, stderr = process.communicate(timeout=60)
    return process.returncode, stdout + stderr


def check_existing(smoke):
    _, state, existing, _, _ = smoke
    assert (existing / "config/genesis.json").read_text() == "original genesis"
    assert (existing / "credentials").read_text() == CREDENTIALS
    assert [p.read_text() for p in (state / "images").iterdir()] == ["existing:local"]
    assert [p.read_text() for p in (state / "projects").iterdir()] == ["existing"]


def calls(smoke):
    return [json.loads(line) for line in smoke[3].read_text().splitlines()]


def wait_for_project(smoke, process):
    deadline = time.monotonic() + 45
    while len(list((smoke[1] / "projects").iterdir())) < 2:
        assert process.poll() is None, finish(process)[1]
        assert time.monotonic() < deadline
        time.sleep(0.05)


def interrupt(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    return finish(process)


def test_resource_identity_is_reported_before_the_flow_finishes(smoke):
    process = start(smoke, "smoke", SMOKE_STUB_WAIT="1", PYTHONUNBUFFERED="")
    try:
        wait_for_project(smoke, process)
        descriptor = process.stdout.fileno()
        os.set_blocking(descriptor, False)
        output = b""
        try:
            while True:
                chunk = os.read(descriptor, 65536)
                if not chunk:
                    break
                output += chunk
        except BlockingIOError:
            pass
        finally:
            os.set_blocking(descriptor, True)
        assert b"Smoke resources:" in output
    finally:
        interrupt(process)
    check_existing(smoke)


def test_source_and_genesis_controls_survive_runtime_sanitization(isolation_module):
    configured = {
        "XIAN_CONFIGS_DIR": "/read-only/configs",
        "XIAN_SMOKE_NETWORK": "review-network",
        "XIAN_SMOKE_TIMEOUT_SECONDS": "120",
        "PATH": "/tools",
    }
    inherited = {
        **configured,
        "XIAN_COMETBFT_HOME": "/existing-node",
        "COMPOSE_PROJECT_NAME": "existing",
        "MAKEFLAGS": "XIAN_COMETBFT_HOME=/existing-node",
    }
    assert isolation_module.clean_environment(inherited) == configured


def test_child_session_is_separate_from_the_calling_process(
    isolation_module, tmp_path, monkeypatch
):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    result = tmp_path / "session.json"
    script = scripts / "smoke-stack-run.sh"
    program = (
        "import json,os; from pathlib import Path; "
        "Path(os.environ['SMOKE_SESSION_FILE']).write_text("
        "json.dumps([os.getpid(),os.getsid(0),os.getpgrp()]))"
    )
    import shlex

    script.write_text(f"exec {shlex.quote(sys.executable)} -c {shlex.quote(program)}\n")
    monkeypatch.setattr(isolation_module, "STACK", tmp_path)
    assert (
        isolation_module.run_child("stack", {**os.environ, "SMOKE_SESSION_FILE": str(result)}) == 0
    )
    pid, session, group = json.loads(result.read_text())
    assert pid == session == group
    assert group != os.getpgrp()


def test_cleanup_does_not_publish_image_inspection_metadata(
    isolation_module, smoke, tmp_path, capfd
):
    env = isolation_module.run_environment(tmp_path / "owned", "stack", smoke[4])
    assert isolation_module.cleanup(env)
    assert "sha256:" + "a" * 64 not in capfd.readouterr().out
    check_existing(smoke)


@pytest.mark.parametrize("target", ["smoke", "smoke-cli"])
def test_smoke_ignores_existing_resources_and_nested_make_overrides(smoke, target):
    code, output = finish(start(smoke, target))
    assert code == 0, output
    check_existing(smoke)
    recorded = calls(smoke)
    assert any(c["tool"] == "docker" and "down" in c["args"] for c in recorded)
    for call in recorded:
        assert call["COMPOSE_PROJECT_NAME"] != "existing"
        assert call["XIAN_COMETBFT_RPC_HOST"] == "127.0.0.1"
        assert call["XIAN_NODE_INTEGRATED_IMAGE"] != "existing:local"
        assert not Path(call["XIAN_COMETBFT_HOME"]).is_relative_to(smoke[2])
    homes = {c["XIAN_COMETBFT_HOME"] for c in recorded}
    assert all(not Path(home).exists() for home in homes)


@pytest.mark.parametrize("target", ["smoke", "smoke-cli"])
@pytest.mark.parametrize("failure", ["build", "startup", "preflight"])
def test_failure_cleanup_does_not_touch_existing_resources(smoke, target, failure):
    code, output = finish(
        start(
            smoke,
            target,
            SMOKE_STUB_FAIL=failure,
            SMOKE_STUB_PREFLIGHT_EXIT="19" if failure == "preflight" else "0",
        )
    )
    assert code != 0, output
    check_existing(smoke)
    assert all(not Path(c["XIAN_COMETBFT_HOME"]).exists() for c in calls(smoke))


def test_simultaneous_smoke_runs_have_distinct_resources(smoke):
    processes = [start(smoke, target) for target in ("smoke", "smoke-cli")]
    for process in processes:
        code, output = finish(process)
        assert code == 0, output
    check_existing(smoke)
    records = calls(smoke)
    projects = {c["COMPOSE_PROJECT_NAME"] for c in records}
    assert len(projects) == 2
    for key in ("XIAN_COMETBFT_HOME", "XIAN_NODE_INTEGRATED_IMAGE", "XIAN_COMETBFT_RPC_PORT"):
        assert len({c[key] for c in records}) == 2


@pytest.mark.parametrize("target", ["smoke", "smoke-cli"])
def test_inherited_image_reuse_settings_cannot_skip_the_private_build(smoke, target):
    code, output = finish(
        start(smoke, target, XIAN_SMOKE_SKIP_BUILD="1", XIAN_SMOKE_IMAGE="existing:local")
    )
    assert code == 0, output
    check_existing(smoke)
    assert any("abci-build" in c["args"] for c in calls(smoke))
    assert not any(c["args"][:1] == ["tag"] for c in calls(smoke))


@pytest.mark.parametrize("target", ["smoke", "smoke-cli"])
def test_interruption_cleans_up_only_the_interrupted_run(smoke, target):
    process = start(smoke, target, SMOKE_STUB_WAIT="1")
    try:
        wait_for_project(smoke, process)
    finally:
        code, output = interrupt(process)
    assert code != 0, output
    check_existing(smoke)
    assert all(not Path(c["XIAN_COMETBFT_HOME"]).exists() for c in calls(smoke))


def test_cleanup_failure_retains_only_owned_resources_for_diagnosis(smoke):
    code, output = finish(start(smoke, "smoke", SMOKE_STUB_FAIL="cleanup"))
    assert code != 0
    assert "retained owned resources" in output
    assert (smoke[2] / "config/genesis.json").read_text() == "original genesis"
    assert resource(smoke[1], "projects", "existing").read_text() == "existing"
    assert resource(smoke[1], "images", "existing:local").read_text() == "existing:local"
    assert all(Path(c["XIAN_COMETBFT_HOME"]).exists() for c in calls(smoke))


@pytest.fixture
def isolation_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "smoke_isolation", ROOT / "scripts/smoke_isolation.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hung_child_is_killed_only_in_its_own_process_group(isolation_module, monkeypatch):
    from unittest.mock import Mock

    child = Mock(pid=12345)
    child.wait.side_effect = [subprocess.TimeoutExpired("owned-child", 10), 0]
    signals = []
    monkeypatch.setattr(isolation_module.os, "killpg", lambda *args: signals.append(args))
    isolation_module.stop_child(child)
    assert signals == [(12345, signal.SIGTERM), (12345, signal.SIGKILL)]
    assert child.wait.call_count == 2


def test_already_exited_child_does_not_signal_another_process(isolation_module, monkeypatch):
    from unittest.mock import Mock

    child = Mock(pid=12345)
    kill = Mock(side_effect=ProcessLookupError)
    monkeypatch.setattr(isolation_module.os, "killpg", kill)
    isolation_module.stop_child(child)
    kill.assert_called_once_with(12345, signal.SIGTERM)
    child.wait.assert_not_called()


def test_invalid_mode_cannot_launch_a_smoke_flow(isolation_module):
    with pytest.raises(ValueError, match="smoke mode"):
        isolation_module.main("existing")


def test_environment_setup_failure_removes_the_private_directory(
    isolation_module, tmp_path, monkeypatch
):
    from unittest.mock import Mock

    owned = tmp_path / "owned"
    owned.mkdir()
    monkeypatch.setattr(isolation_module.tempfile, "mkdtemp", lambda **_: str(owned))
    monkeypatch.setattr(isolation_module.signal, "signal", Mock())
    monkeypatch.setattr(
        isolation_module, "initialize_environment", Mock(side_effect=ValueError("bad setup"))
    )
    with pytest.raises(ValueError, match="bad setup"):
        isolation_module.main("stack")
    assert not owned.exists()


@pytest.mark.parametrize("image_exists, remove_exit", [(False, 0), (True, 17)])
def test_cleanup_only_removes_present_owned_images(
    isolation_module, monkeypatch, image_exists, remove_exit
):
    from unittest.mock import Mock

    env = {
        "COMPOSE_PROJECT_NAME": "owned-project",
        "XIAN_NODE_INTEGRATED_IMAGE": "owned:node",
        "XIAN_SMOKE_ROOT": "/tmp/xian-smoke-stack-owned",
    }
    # down, inspect, then (image present) ownership return and removal
    exits = [0, 0, 0, remove_exit] if image_exists else [0, 1]
    run = Mock(side_effect=[subprocess.CompletedProcess([], code) for code in exits])
    monkeypatch.setattr(isolation_module.subprocess, "run", run)
    assert isolation_module.cleanup(env) is (not image_exists)
    commands = [call.args[0] for call in run.call_args_list]
    assert commands[0][commands[0].index("--project-name") + 1] == "owned-project"
    removals = [cmd for cmd in commands if cmd[:3] == ["docker", "image", "rm"]]
    assert removals == ([["docker", "image", "rm", "owned:node"]] if image_exists else [])


def test_cleanup_returns_container_written_files_before_removing_the_image(
    isolation_module, monkeypatch
):
    """The node container runs as root and writes into the bind-mounted smoke root;
    on Linux the runner could not delete those files afterwards."""
    from unittest.mock import Mock

    env = {
        "COMPOSE_PROJECT_NAME": "owned-project",
        "XIAN_NODE_INTEGRATED_IMAGE": "owned:node",
        "XIAN_SMOKE_ROOT": "/tmp/xian-smoke-stack-owned",
    }
    run = Mock(side_effect=[subprocess.CompletedProcess([], code) for code in (0, 0, 0, 0)])
    monkeypatch.setattr(isolation_module.subprocess, "run", run)
    monkeypatch.setattr(isolation_module.os, "getuid", lambda: 1001)
    monkeypatch.setattr(isolation_module.os, "getgid", lambda: 121)
    assert isolation_module.cleanup(env) is True
    commands = [call.args[0] for call in run.call_args_list]
    chown = [
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "chown",
        "-v",
        "/tmp/xian-smoke-stack-owned:/smoke",
        "owned:node",
        "-R",
        "1001:121",
        "/smoke",
    ]
    assert commands.index(chown) < commands.index(["docker", "image", "rm", "owned:node"])


def test_cleanup_keeps_the_image_when_ownership_cannot_be_returned(isolation_module, monkeypatch):
    from unittest.mock import Mock

    env = {
        "COMPOSE_PROJECT_NAME": "owned-project",
        "XIAN_NODE_INTEGRATED_IMAGE": "owned:node",
        "XIAN_SMOKE_ROOT": "/tmp/xian-smoke-stack-owned",
    }
    run = Mock(side_effect=[subprocess.CompletedProcess([], code) for code in (0, 0, 1)])
    monkeypatch.setattr(isolation_module.subprocess, "run", run)
    assert isolation_module.cleanup(env) is False
    commands = [call.args[0] for call in run.call_args_list]
    assert not [cmd for cmd in commands if cmd[:3] == ["docker", "image", "rm"]]


def test_shutdown_ignores_repeat_signals_until_owned_resources_are_cleaned(isolation_module):
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        with pytest.raises(SystemExit) as stopped:
            isolation_module.interrupted(signal.SIGTERM, None)
        assert stopped.value.code == 128 + signal.SIGTERM
        assert signal.getsignal(signal.SIGTERM) == signal.SIG_IGN
        assert signal.getsignal(signal.SIGINT) == signal.SIG_IGN
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
