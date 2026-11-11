"""Process-entry boundaries preserve literal data and propagate failures."""

import os
import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import configure_node
import stack_credentials as credentials


def test_credential_stdout_protocol_preserves_trailing_newlines(tmp_path, capsys):
    path = tmp_path / "secrets.env"
    expected = {"XIAN_BDS_PASSWORD": "line\n", "XIAN_POSTGRAPHILE_PASSWORD": "quoted'\n"}
    credentials.write_credentials(path, expected)
    with patch.dict(os.environ, {}, clear=True):
        assert credentials.main(["--path", str(path)]) == 0
    assert capsys.readouterr().out == "line\n\0quoted'\n\0"


def test_default_credential_location_is_beside_the_stack_scripts(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(credentials, "__file__", str(tmp_path / "scripts/stack_credentials.py"))
    with patch.dict(
        os.environ,
        {"XIAN_BDS_PASSWORD": "placeholder", "XIAN_POSTGRAPHILE_PASSWORD": "other"},
        clear=True,
    ):
        assert credentials.main([]) == 0
    assert (tmp_path / ".stack-secrets.env").is_file()
    assert capsys.readouterr().out == "placeholder\0other\0"


def test_credential_command_entrypoint_passes_literal_environment(tmp_path):
    path = tmp_path / "secrets.env"
    expected = {"XIAN_BDS_PASSWORD": "line\n", "XIAN_POSTGRAPHILE_PASSWORD": "other"}
    credentials.write_credentials(path, expected)

    def record_exec(command, arguments, environment):
        assert command == "recorder"
        assert arguments == ["recorder", "literal' argument"]
        assert {key: environment[key] for key in expected} == expected
        raise SystemExit(0)

    with patch.dict(os.environ, {}, clear=True), patch.object(os, "execvpe", record_exec):
        with pytest.raises(SystemExit) as exit_info:
            credentials.main(["--path", str(path), "--", "recorder", "literal' argument"])
    assert exit_info.value.code == 0


@pytest.mark.parametrize("arguments", [["--"], ["--", "/nonexistent/credential-test-command"]])
def test_credential_command_errors_are_reported_without_values(tmp_path, capsys, arguments):
    path = tmp_path / "secrets.env"
    with patch.dict(
        os.environ,
        {"XIAN_BDS_PASSWORD": "private-placeholder", "XIAN_POSTGRAPHILE_PASSWORD": "other"},
        clear=True,
    ):
        assert credentials.main(["--path", str(path), *arguments]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "xian-stack credentials:" in captured.err
    assert "private-placeholder" not in captured.err


@pytest.mark.parametrize("filled_optional", [False, True])
def test_configure_entrypoint_preserves_arguments_and_child_exit(tmp_path, filled_optional, capsys):
    environment = {key: "value" for key, _option in configure_node.VALUE_OPTIONS}
    environment.update({key: "0" for key, *_flags in configure_node.BOOLEAN_OPTIONS})
    environment.update(
        {
            key: "literal' value" if filled_optional else ""
            for key, _ in configure_node.OPTIONAL_OPTIONS
        }
    )
    environment.update(
        XIAN_STACK_SECRETS_ENV=str(tmp_path / "secrets.env"),
        XIAN_BDS_PASSWORD="--literal' password\n",
        XIAN_POSTGRAPHILE_PASSWORD="other",
        CONFIGURE_ARGS=shlex.join(["--moniker", "literal $(printf unchanged)"]),
        DOCKER_COMPOSE="recorder --compose-mode",
    )

    def record_run(command, *, env, check):
        assert command[:2] == ["recorder", "--compose-mode"]
        assert command[command.index("--entrypoint") + 1] == "xian-configure-node"
        assert "--bds-password=--literal' password\n" in command
        assert command[-2:] == ["--moniker", "literal $(printf unchanged)"]
        assert env["XIAN_BDS_PASSWORD"] == environment["XIAN_BDS_PASSWORD"]
        assert check is False
        return subprocess.CompletedProcess(command, 23)

    with (
        patch.dict(os.environ, environment, clear=True),
        patch.object(subprocess, "run", record_run),
    ):
        assert configure_node.main() == 23
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("options", [{"DOCKER_COMPOSE": ""}, {"CONFIGURE_ARGS": "'unclosed"}])
def test_bad_configuration_options_fail_before_credential_writes(tmp_path, options, capsys):
    path = tmp_path / "secrets.env"
    with patch.dict(os.environ, {"XIAN_STACK_SECRETS_ENV": str(path), **options}, clear=True):
        assert configure_node.main() == 1
    assert not path.exists()
    assert "xian-stack node configuration:" in capsys.readouterr().err


@pytest.mark.parametrize("value", [None, 1, True, "nul\0value", "\ud800"])
def test_writer_rejects_unrepresentable_credential_values(tmp_path, value):
    path = tmp_path / "secrets.env"
    with pytest.raises(ValueError):
        credentials.write_credentials(
            path, {"XIAN_BDS_PASSWORD": value, "XIAN_POSTGRAPHILE_PASSWORD": "other"}
        )
    assert not path.exists()


def test_writer_rejects_missing_key_without_writing_a_partial_file(tmp_path):
    path = tmp_path / "secrets.env"
    with pytest.raises(ValueError, match="exactly the two"):
        credentials.write_credentials(path, {"XIAN_BDS_PASSWORD": "placeholder"})
    assert not path.exists()


def test_reader_rejects_directories_and_fifos_without_blocking(tmp_path):
    with pytest.raises(ValueError, match="regular file"):
        credentials.read_credentials(tmp_path)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="regular file"):
        credentials.read_credentials(fifo)


def test_atomic_write_failure_before_replace_leaves_no_partial_file(tmp_path):
    path = tmp_path / "secrets.env"
    with patch.object(os, "fsync", side_effect=OSError("simulated sync failure")):
        with pytest.raises(OSError, match="sync failure"):
            credentials.write_credentials(
                path, {"XIAN_BDS_PASSWORD": "placeholder", "XIAN_POSTGRAPHILE_PASSWORD": "other"}
            )
    assert not path.exists()
    assert list(tmp_path.iterdir()) == [path.with_name(path.name + ".lock")]
