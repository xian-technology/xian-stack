from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import dex_automation_backend
import pytest


@pytest.mark.parametrize("response", ["status", "config"])
@pytest.mark.parametrize(
    ("credentials", "expected_kind", "expected_source"),
    [
        ({}, "file", "config/keeper.key"),
        ({"CUSTOM_FILE": "override.key"}, "file", "override.key"),
        (
            {"CUSTOM_KEY": "secret-sentinel", "CUSTOM_FILE": "override.key"},
            "environment",
            "CUSTOM_KEY",
        ),
    ],
)
def test_metadata_matches_config_owned_runtime_source(
    tmp_path, monkeypatch, response, credentials, expected_kind, expected_source
):
    config_path = tmp_path / "config/automation.yaml"
    config_path.parent.mkdir()
    config_path.write_text(
        "wallet:\n  private_key_file: keeper.key\n"
        "  private_key_env: CUSTOM_KEY\n  private_key_file_env: CUSTOM_FILE\n"
    )
    monkeypatch.setattr(dex_automation_backend, "_PROCESS_DIR", tmp_path)
    monkeypatch.setattr(dex_automation_backend, "_PID_PATH", tmp_path / "absent.pid")
    monkeypatch.setattr(dex_automation_backend, "STACK_DIR", tmp_path)
    env = {
        **credentials,
        "XIAN_DEX_AUTOMATION_CONFIG": str(config_path),
        "XIAN_DEX_AUTOMATION_DIR": str(Path(__file__).resolve().parents[2] / "xian-dex-automation"),
        "XIAN_PY_DIR": str(Path(__file__).resolve().parents[2] / "xian-py"),
    }
    if response == "status":
        result = dex_automation_backend.get_dex_automation_status(
            bind_host="127.0.0.1",
            port=38280,
            env=env,
        )
    else:
        result = dex_automation_backend.ensure_dex_automation_config(
            rpc_url="http://127.0.0.1:26657",
            env=env,
        )
    expected = str(tmp_path / expected_source) if expected_kind == "file" else expected_source
    assert result["dex_automation_credential_source"] == {"kind": expected_kind, "source": expected}
    assert result["dex_automation_wallet_key_path"] == (
        expected if expected_kind == "file" else None
    )
    assert result["dex_automation_credential_source_error"] is None
    assert "secret-sentinel" not in json.dumps(result)
    assert not (tmp_path / "wallet.key").exists()


@pytest.mark.parametrize("contents", [None, "wallet: [secret-sentinel]", "{}"])
def test_status_missing_invalid_or_unconfigured_custody_is_not_a_default_file(
    tmp_path, monkeypatch, contents
):
    path = tmp_path / "config.yaml"
    if contents is not None:
        path.write_text(contents)
    monkeypatch.setattr(dex_automation_backend, "_PID_PATH", tmp_path / "absent.pid")
    env = {"XIAN_DEX_AUTOMATION_CONFIG": str(path)}
    if contents is None:
        monkeypatch.setattr(
            dex_automation_backend.subprocess,
            "run",
            lambda *_a, **_k: pytest.fail("missing config must not launch uv"),
        )
    result = dex_automation_backend.get_dex_automation_status(
        bind_host="127.0.0.1",
        port=38280,
        env=env,
    )
    assert result["dex_automation_wallet_key_path"] is None
    assert result["dex_automation_credential_source"] == {"kind": None, "source": None}
    assert bool(result["dex_automation_credential_source_error"]) == (contents != "{}")
    assert "secret-sentinel" not in json.dumps(result)


def test_status_absent_optional_repository_does_not_launch_dependencies(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("{}")
    monkeypatch.setattr(dex_automation_backend, "_PID_PATH", tmp_path / "absent.pid")
    monkeypatch.setattr(
        dex_automation_backend.subprocess,
        "run",
        lambda *_a, **_k: pytest.fail("missing repo must not launch uv"),
    )
    result = dex_automation_backend.get_dex_automation_status(
        bind_host="127.0.0.1",
        port=38280,
        env={
            "XIAN_DEX_AUTOMATION_CONFIG": str(path),
            "XIAN_DEX_AUTOMATION_DIR": str(tmp_path / "missing-repo"),
        },
    )
    assert result["dex_automation_wallet_key_path"] is None
    assert result["dex_automation_credential_source_error"] == "credential source unavailable"


@pytest.mark.parametrize("environment", [None, "custom-env"])
def test_status_uses_only_existing_service_interpreter(tmp_path, monkeypatch, environment):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname='xian-dex-automation'\n")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("{}")
    env = {
        "XIAN_DEX_AUTOMATION_DIR": str(repo),
        "XIAN_DEX_AUTOMATION_CONFIG": str(config_path),
    }
    if environment:
        env["UV_PROJECT_ENVIRONMENT"] = environment
    interpreter = repo / (environment or ".venv") / "bin/python"
    monkeypatch.setattr(dex_automation_backend, "_PID_PATH", tmp_path / "absent.pid")
    run = unittest.mock.Mock(return_value=SimpleNamespace(stdout='{"kind":null,"source":null}'))
    monkeypatch.setattr(dex_automation_backend.subprocess, "run", run)
    result = dex_automation_backend.get_dex_automation_status(
        bind_host="127.0.0.1",
        port=38280,
        env=env,
    )
    run.assert_not_called()
    assert result["dex_automation_credential_source_error"]
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    result = dex_automation_backend.get_dex_automation_status(
        bind_host="127.0.0.1",
        port=38280,
        env=env,
    )
    assert result["dex_automation_credential_source_error"] is None
    command = run.call_args.args[0]
    assert command == [
        str(interpreter),
        "-B",
        "-m",
        "xian_dex_automation.credentials",
        "--config",
        str(config_path),
    ]
    assert run.call_args.kwargs["cwd"] == dex_automation_backend.STACK_DIR


@pytest.mark.parametrize(
    ("payload", "exit_code"),
    [
        ({"kind": "unexpected", "source": "secret-sentinel"}, 0),
        ({"kind": None, "source": "secret-sentinel"}, 0),
        ({"kind": "file", "source": None}, 0),
        ({"kind": "environment", "source": 123}, 0),
        ({"kind": "environment", "source": "CUSTOM_KEY"}, 1),
    ],
)
def test_metadata_rejects_invalid_inspection_output(tmp_path, monkeypatch, payload, exit_code):
    repo = tmp_path / "repo"
    interpreter = repo / ".venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\nname='xian-dex-automation'\n")
    interpreter.write_text(
        "#!/bin/sh\nprintf '%s' '" + json.dumps(payload) + f"'\nexit {exit_code}\n"
    )
    interpreter.chmod(0o700)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("{}")
    monkeypatch.setattr(dex_automation_backend, "_PID_PATH", tmp_path / "absent.pid")
    result = dex_automation_backend.get_dex_automation_status(
        bind_host="127.0.0.1",
        port=38280,
        env={"XIAN_DEX_AUTOMATION_DIR": str(repo), "XIAN_DEX_AUTOMATION_CONFIG": str(config_path)},
    )
    assert result["dex_automation_wallet_key_path"] is None
    assert result["dex_automation_credential_source_error"] == "credential source unavailable"
    assert "secret-sentinel" not in json.dumps(result)


@pytest.mark.parametrize("contents", ["", " \n\t"])
def test_existing_empty_key_is_not_replaced(tmp_path, contents):
    path = tmp_path / "wallet.key"
    path.write_text(contents)
    with pytest.raises(RuntimeError, match="empty.*restore"):
        dex_automation_backend._ensure_wallet_key(path)
    assert path.read_text() == contents


def test_existing_config_does_not_generate_a_default_key(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    contents = "wallet:\n  private_key_file: existing-keeper.key\n"
    config_path.write_text(contents)
    default_key = tmp_path / "wallet.key"
    monkeypatch.setattr(dex_automation_backend, "require_dex_automation_repo", lambda **_: tmp_path)
    monkeypatch.setattr(dex_automation_backend, "_PROCESS_DIR", tmp_path)
    dex_automation_backend.ensure_dex_automation_config(
        rpc_url="http://127.0.0.1:26657",
        env={
            "XIAN_DEX_AUTOMATION_CONFIG": str(config_path),
            "XIAN_DEX_AUTOMATION_PRIVATE_KEY_FILE": str(default_key),
        },
    )
    assert not default_key.exists()
    assert config_path.read_text() == contents


@pytest.mark.parametrize("configured", [None, "/explicit/keeper.key"])
def test_runtime_env_preserves_explicit_key_source_without_injecting_one(monkeypatch, configured):
    variable = "XIAN_DEX_AUTOMATION_PRIVATE_KEY_FILE"
    monkeypatch.delenv(variable, raising=False)
    supplied = {} if configured is None else {variable: configured}
    actual = dex_automation_backend._build_runtime_env(supplied).get(variable)
    assert actual == configured


@pytest.mark.parametrize("configured", [None, "/explicit/keeper.key"])
def test_shell_env_preserves_explicit_key_source_without_injecting_one(configured):
    variable = "XIAN_DEX_AUTOMATION_PRIVATE_KEY_FILE"
    env = {k: v for k, v in os.environ.items() if k != variable}
    if configured is not None:
        env[variable] = configured
    result = subprocess.run(
        [
            "bash",
            "-c",
            "source scripts/stack-env.sh; export_stack_env; "
            'printf "%s" "${XIAN_DEX_AUTOMATION_PRIVATE_KEY_FILE:-}"',
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout.strip() == (configured or "")


class DexAutomationBackendTests(unittest.TestCase):
    def test_endpoints_use_loopback_display_for_public_host(self) -> None:
        endpoints = dex_automation_backend.dex_automation_endpoints(
            bind_host="0.0.0.0",
            port=38280,
        )

        self.assertEqual(
            "http://127.0.0.1:38280",
            endpoints["dex_automation"],
        )
        self.assertEqual(
            "http://127.0.0.1:38280/health",
            endpoints["dex_automation_health"],
        )

    def test_endpoints_bracket_ipv6_hosts(self) -> None:
        endpoints = dex_automation_backend.dex_automation_endpoints(
            bind_host="::1",
            port=38280,
        )

        self.assertEqual("http://[::1]:38280", endpoints["dex_automation"])
        self.assertEqual(
            "http://[::1]:38280/health",
            endpoints["dex_automation_health"],
        )

    def test_ensure_config_generates_wallet_and_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "xian-dex-automation"
            repo.mkdir()
            (repo / "pyproject.toml").write_text(
                "[project]\nname='xian-dex-automation'\n",
                encoding="utf-8",
            )
            process_dir = root / "artifacts"
            config_path = process_dir / "config.yaml"
            wallet_path = process_dir / "wallet.key"
            env = {
                "XIAN_DEX_AUTOMATION_DIR": str(repo),
                "XIAN_DEX_AUTOMATION_CONFIG": str(config_path),
                "XIAN_DEX_AUTOMATION_PRIVATE_KEY_FILE": str(wallet_path),
            }

            with patch.object(
                dex_automation_backend,
                "_PROCESS_DIR",
                process_dir,
            ):
                result = dex_automation_backend.ensure_dex_automation_config(
                    rpc_url="http://127.0.0.1:26657",
                    env=env,
                )

            self.assertEqual(
                str(repo.resolve()),
                result["dex_automation_repo_dir"],
            )
            self.assertTrue(config_path.exists())
            self.assertTrue(wallet_path.exists())
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(
                "http://127.0.0.1:26657",
                payload["network"]["rpc_url"],
            )
            self.assertEqual(
                str(wallet_path.resolve()),
                payload["wallet"]["private_key_file"],
            )

    def test_status_removes_stale_pid_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            process_dir = Path(temp_dir)
            pid_path = process_dir / "dex-automation.pid"
            log_path = process_dir / "dex-automation.log"
            pid_path.write_text("12345", encoding="utf-8")

            with patch.object(dex_automation_backend, "_PID_PATH", pid_path):
                with patch.object(dex_automation_backend, "_LOG_PATH", log_path):
                    with patch.object(
                        dex_automation_backend,
                        "_process_running",
                        return_value=False,
                    ):
                        status = dex_automation_backend.get_dex_automation_status(
                            bind_host="127.0.0.1",
                            port=38280,
                        )

            self.assertFalse(status["dex_automation_running"])
            self.assertIsNone(status["dex_automation_pid"])
            self.assertFalse(pid_path.exists())

    def test_start_cleans_up_pid_file_when_readiness_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            process_dir = Path(temp_dir)
            pid_path = process_dir / "dex-automation.pid"
            log_path = process_dir / "dex-automation.log"
            env = {
                "XIAN_DEX_AUTOMATION_DIR": str(process_dir),
                "XIAN_DEX_AUTOMATION_CONFIG": str(process_dir / "config.yaml"),
                "XIAN_DEX_AUTOMATION_PRIVATE_KEY_FILE": str(process_dir / "wallet.key"),
                "XIAN_PY_DIR": str(process_dir / "xian-py"),
            }

            with patch.object(dex_automation_backend, "_PROCESS_DIR", process_dir):
                with patch.object(dex_automation_backend, "_PID_PATH", pid_path):
                    with patch.object(dex_automation_backend, "_LOG_PATH", log_path):
                        with patch.object(
                            dex_automation_backend,
                            "ensure_dex_automation_config",
                            return_value={},
                        ):
                            with patch.object(
                                dex_automation_backend.subprocess,
                                "Popen",
                                return_value=SimpleNamespace(pid=12345),
                            ):
                                with patch.object(
                                    dex_automation_backend,
                                    "_process_running",
                                    return_value=False,
                                ):
                                    with patch.object(
                                        dex_automation_backend,
                                        "_wait_for_ready",
                                        side_effect=TimeoutError("port unavailable"),
                                    ):
                                        with self.assertRaisesRegex(
                                            TimeoutError,
                                            "port unavailable",
                                        ):
                                            dex_automation_backend.start_dex_automation_runtime(
                                                bind_host="127.0.0.1",
                                                port=38280,
                                                rpc_url="http://127.0.0.1:26657",
                                                env=env,
                                            )

            self.assertFalse(pid_path.exists())


if __name__ == "__main__":
    unittest.main()
