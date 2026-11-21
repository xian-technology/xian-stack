"""Credential persistence and shell/bootstrap consumers treat values as data.

The stack credentials file holds the PostGraphile password. The BDS password
lives only in its own owner-only file (XIAN_BDS_PASSWORD_FILE), which Compose
mounts as /run/secrets/xian_bds_password for Postgres and the node.
"""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).absolute().parents[1] / "scripts"))

import stack_credentials as credentials

ROOT = Path(__file__).absolute().parents[1]
KEY = "XIAN_POSTGRAPHILE_PASSWORD"
VALUES = [
    "simple-placeholder",
    'apostrophe\'s "quoted" value\\end',
    "literal-$(printf changed)-`printf changed`-$HOME",
    " first line\nsecond line\n",
]


def shell_credentials(path, **overrides):
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("XIAN_BDS_")
    }
    return subprocess.run(
        [
            "bash",
            "-c",
            "source ./scripts/stack-env.sh && ensure_stack_secrets_env && "
            "python3 -c 'import json,os; print(json.dumps({k:os.environ[k] for k in "
            '["XIAN_POSTGRAPHILE_PASSWORD","XIAN_BDS_PASSWORD_FILE"]}))\'',
        ],
        cwd=ROOT,
        env={**environment, "XIAN_STACK_SECRETS_ENV": str(path), **overrides},
        capture_output=True,
        text=True,
        check=False,
    )


def write_bds_password(path: Path, value: str, mode: int = 0o600) -> Path:
    path.write_text(value, encoding="utf-8")
    path.chmod(mode)
    return path


@pytest.mark.parametrize("value", VALUES)
def test_backend_written_credentials_roundtrip_through_shell(tmp_path, value):
    path = tmp_path / "secrets.env"
    credentials.write_credentials(path, {KEY: value})
    assert credentials.read_credentials(path) == {KEY: value}
    result = shell_credentials(path, **{KEY: ""})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        KEY: value,
        "XIAN_BDS_PASSWORD_FILE": str(tmp_path / ".stack-bds-password"),
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_first_use_generates_an_owner_only_bds_password_file(tmp_path):
    path = tmp_path / "secrets.env"

    values = credentials.ensure_credentials(path, {})

    password_file = tmp_path / ".stack-bds-password"
    assert values == {KEY: values[KEY], "XIAN_BDS_PASSWORD_FILE": str(password_file)}
    assert stat.S_IMODE(password_file.stat().st_mode) == 0o600
    password = password_file.read_text(encoding="utf-8")
    assert len(password) >= 32
    assert password not in path.read_text(encoding="utf-8")
    assert password != values[KEY]


def test_bds_password_file_in_a_new_directory_is_created_privately(tmp_path):
    password_file = tmp_path / "private" / "nested" / "bds-password"

    values = credentials.ensure_credentials(
        tmp_path / "secrets.env", {"XIAN_BDS_PASSWORD_FILE": str(password_file)}
    )

    assert values["XIAN_BDS_PASSWORD_FILE"] == str(password_file)
    assert stat.S_IMODE(password_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(password_file.parent.stat().st_mode) == 0o700


def test_existing_bds_password_file_is_used_unchanged(tmp_path):
    password_file = write_bds_password(tmp_path / "chosen-password", "operator value\n")

    values = credentials.ensure_credentials(
        tmp_path / "secrets.env", {"XIAN_BDS_PASSWORD_FILE": str(password_file)}
    )

    assert values["XIAN_BDS_PASSWORD_FILE"] == str(password_file)
    assert password_file.read_text(encoding="utf-8") == "operator value\n"
    assert credentials.read_bds_password(password_file) == "operator value"


@pytest.mark.parametrize(
    ("content", "mode", "message"),
    [
        ("secret", 0o640, "owner"),
        ("secret", 0o604, "owner"),
        ("\n", 0o600, "empty"),
        ("xian", 0o600, "weak default value 'xian'"),
    ],
)
def test_unsafe_bds_password_files_are_refused(tmp_path, content, mode, message):
    password_file = write_bds_password(tmp_path / ".stack-bds-password", content, mode)

    with pytest.raises(ValueError, match=message):
        credentials.ensure_credentials(tmp_path / "secrets.env", {})
    assert password_file.read_text(encoding="utf-8") == content


def test_bds_password_must_differ_from_the_postgraphile_password(tmp_path):
    write_bds_password(tmp_path / ".stack-bds-password", "shared-value")

    with pytest.raises(ValueError, match="XIAN_POSTGRAPHILE_PASSWORD must differ"):
        credentials.ensure_credentials(tmp_path / "secrets.env", {KEY: "shared-value"})


def test_bds_password_symlink_is_rejected_without_reading_the_target(tmp_path):
    target = write_bds_password(tmp_path / "target", "untouched-placeholder")
    (tmp_path / ".stack-bds-password").symlink_to(target)

    with pytest.raises((ValueError, OSError)):
        credentials.ensure_credentials(tmp_path / "secrets.env", {})
    assert target.read_text(encoding="utf-8") == "untouched-placeholder"


def test_unknown_credential_key_is_rejected_without_disclosing_its_value(tmp_path):
    path = tmp_path / "secrets.env"
    source = (
        "# xian-stack-credentials-v1\n"
        'XIAN_BDS_PASSWORD="old-placeholder"\n'
        'XIAN_POSTGRAPHILE_PASSWORD="other"\n'
    )
    path.write_text(source)

    with pytest.raises(ValueError, match="invalid credential assignment on line 2"):
        credentials.ensure_credentials(path, {})
    result = shell_credentials(path)
    assert result.returncode != 0
    assert "invalid credential assignment on line 2" in result.stderr
    assert "old-placeholder" not in result.stderr
    assert path.read_text() == source
    assert not (tmp_path / ".stack-bds-password").exists()


@pytest.mark.parametrize("variable", ["XIAN_BDS_UNKNOWN", "XIAN_BDS_TYPO"])
def test_unknown_bds_environment_settings_are_refused_by_name(tmp_path, variable):
    with pytest.raises(ValueError, match=rf"unknown stack BDS variable\(s\): {variable}"):
        credentials.ensure_credentials(tmp_path / "secrets.env", {variable: ""})
    assert not (tmp_path / "secrets.env").exists()


def test_stack_operations_variables_are_not_bds_settings(tmp_path):
    environment = {
        "XIAN_BDS_ENABLED": "1",
        "XIAN_BDS_DATA_DIR": str(tmp_path / "bds"),
        "XIAN_BDS_SNAPSHOT_PATH": str(tmp_path / "snapshot.tar.gz"),
    }

    assert credentials.ensure_credentials(tmp_path / "secrets.env", environment)[KEY]


def test_credentials_require_the_versioned_format(tmp_path):
    path = tmp_path / "unversioned.env"
    source = 'XIAN_POSTGRAPHILE_PASSWORD="placeholder"\n'
    path.write_text(source)
    with pytest.raises(ValueError, match="credential format header"):
        credentials.read_credentials(path)
    assert path.read_text() == source


@pytest.mark.parametrize(
    "source",
    [
        "not-an-assignment\n",
        "UNKNOWN_SECRET=placeholder\n",
        'XIAN_POSTGRAPHILE_PASSWORD="unterminated\n',
        "XIAN_POSTGRAPHILE_PASSWORD='unterminated\n",
        "XIAN_POSTGRAPHILE_PASSWORD=first\nXIAN_POSTGRAPHILE_PASSWORD=second\n",
        '# xian-stack-credentials-v1\nXIAN_POSTGRAPHILE_PASSWORD="bad\\u0000value"\n',
        '# xian-stack-credentials-v2\nXIAN_POSTGRAPHILE_PASSWORD="placeholder"\n',
        '# legacy comment\n# xian-stack-credentials-v1\nXIAN_POSTGRAPHILE_PASSWORD="placeholder"\n',
    ],
)
def test_malformed_credentials_fail_before_shell_consumer_continues(tmp_path, source):
    path = tmp_path / "invalid.env"
    path.write_text(source)
    with pytest.raises(ValueError):
        credentials.read_credentials(path)
    result = shell_credentials(path, **{KEY: ""})
    assert result.returncode != 0
    assert result.stdout == ""
    assert path.read_text() == source


def test_failed_atomic_write_preserves_previous_credentials(tmp_path, monkeypatch):
    path = tmp_path / "secrets.env"
    credentials.write_credentials(path, {KEY: "previous"})

    def fail_replace(*_args):
        raise OSError("simulated replacement failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replacement failure"):
        credentials.write_credentials(path, {KEY: "new"})
    assert credentials.read_credentials(path) == {KEY: "previous"}
    assert set(tmp_path.iterdir()) <= {path, path.with_name(path.name + ".lock")}


def test_failed_bds_password_write_leaves_no_partial_file(tmp_path, monkeypatch):
    def fail_replace(*_args):
        raise OSError("simulated replacement failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replacement failure"):
        credentials.ensure_credentials(tmp_path / "secrets.env", {})
    assert not (tmp_path / ".stack-bds-password").exists()
    assert not [item for item in tmp_path.iterdir() if ".stack-bds-password." in item.name]


def test_credential_symlink_is_rejected_without_touching_target(tmp_path):
    target = tmp_path / "target"
    target.write_text("untouched-placeholder")
    path = tmp_path / "secrets.env"
    path.symlink_to(target)
    with pytest.raises((ValueError, OSError)):
        credentials.read_credentials(path)
    with pytest.raises((ValueError, OSError)):
        credentials.write_credentials(path, {KEY: "new"})
    assert target.read_text() == "untouched-placeholder"


def test_concurrent_first_use_generates_one_shared_credential_set(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    path = tmp_path / "secrets.env"
    barrier = Barrier(8)

    def load():
        barrier.wait()
        values = credentials.ensure_credentials(path, {})
        return values, credentials.read_bds_password(Path(values["XIAN_BDS_PASSWORD_FILE"]))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _index: load(), range(8)))
    assert all(result == results[0] for result in results)
    values, bds_password = results[0]
    assert credentials.read_credentials(path) == {KEY: values[KEY]}
    assert bds_password != values[KEY]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_command_consumer_receives_literal_environment(tmp_path):
    path = tmp_path / "secrets.env"
    credentials.write_credentials(path, {KEY: VALUES[-1]})
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("XIAN_BDS_")
    }
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/stack_credentials.py"),
            "--path",
            str(path),
            "--",
            sys.executable,
            "-c",
            "import os,json; print(json.dumps({k:os.environ.get(k) for k in "
            '["XIAN_POSTGRAPHILE_PASSWORD","XIAN_BDS_PASSWORD_FILE","XIAN_BDS_PASSWORD"]}))',
        ],
        env={**environment, KEY: ""},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        KEY: VALUES[-1],
        "XIAN_BDS_PASSWORD_FILE": str(tmp_path / ".stack-bds-password"),
        "XIAN_BDS_PASSWORD": None,
    }


def test_failed_file_inspection_closes_the_open_descriptor(tmp_path, monkeypatch):
    path = tmp_path / "secrets.env"
    path.write_text("XIAN_POSTGRAPHILE_PASSWORD=placeholder\n")
    opened = []
    original_open = os.open
    original_fstat = os.fstat

    def record_open(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def fail_fstat(_descriptor):
        raise OSError("simulated file inspection failure")

    monkeypatch.setattr(os, "open", record_open)
    monkeypatch.setattr(os, "fstat", fail_fstat)
    with pytest.raises(OSError, match="inspection failure"):
        credentials.read_credentials(path)
    with pytest.raises(OSError):
        original_fstat(opened[0])


def test_empty_values_are_generated_in_the_current_format(tmp_path):
    path = tmp_path / "secrets.env"
    credentials.write_credentials(path, {KEY: ""})
    assert credentials.read_credentials(path) == {KEY: ""}
    values = credentials.ensure_credentials(path, {})
    assert values[KEY]
    assert credentials.read_credentials(path) == {KEY: values[KEY]}


def test_invalid_assignment_reports_its_source_line(tmp_path):
    path = tmp_path / "secrets.env"
    path.write_text("# xian-stack-credentials-v1\ninvalid assignment\n")
    with pytest.raises(ValueError, match="line 2"):
        credentials.read_credentials(path)


def test_nested_credential_directory_is_created_privately(tmp_path):
    path = tmp_path / "nested" / "private" / "secrets.env"
    credentials.write_credentials(path, {KEY: "placeholder"})
    assert credentials.read_credentials(path) == {KEY: "placeholder"}
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize(
    "compose_variable",
    [
        "STACK_COMPOSE",
        "ABCI_COMPOSE",
        "ABCI_BDS_COMPOSE",
        "ABCI_DEV_COMPOSE",
        "CONTRACTING_COMPOSE",
        "LOCALNET_COMPOSE",
    ],
)
def test_make_compose_families_receive_decoded_environment(tmp_path, compose_variable):
    path = tmp_path / "private store" / "quoted' credential file.env"
    credentials.write_credentials(path, {KEY: "literal$HOME-`text`"})
    recorder = tmp_path / "record compose.py"
    recorder.write_text(
        "import os,json\n"
        "print(json.dumps({key:os.environ.get(key) for key in "
        "['XIAN_POSTGRAPHILE_PASSWORD','XIAN_BDS_PASSWORD_FILE','XIAN_BDS_PASSWORD']}))\n"
    )
    probe = tmp_path / "probe.mk"
    probe.write_text(f"credential-probe:\n\t@$({compose_variable}) config\n")
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("XIAN_BDS_")
    }
    result = subprocess.run(
        ["make", "--no-print-directory", "-f", "Makefile", "-f", str(probe), "credential-probe"],
        cwd=ROOT,
        env={
            **environment,
            "XIAN_STACK_SECRETS_ENV": str(path),
            KEY: "",
            "DOCKER_COMPOSE": f"{sys.executable} '{recorder}'",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        KEY: "literal$HOME-`text`",
        "XIAN_BDS_PASSWORD_FILE": str(path.parent / ".stack-bds-password"),
        "XIAN_BDS_PASSWORD": None,
    }


def test_make_help_does_not_create_or_load_credentials(tmp_path):
    path = tmp_path / "secrets.env"
    result = subprocess.run(
        ["make", "--no-print-directory", "help"],
        cwd=ROOT,
        env={**os.environ, "XIAN_STACK_SECRETS_ENV": str(path), KEY: ""},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not path.exists()
    assert not path.with_name(path.name + ".lock").exists()
    assert not path.with_name(".stack-bds-password").exists()


def test_cli_prints_current_credentials_as_null_separated_values(
    tmp_path, monkeypatch, capfdbinary
):
    path = tmp_path / "secrets.env"
    monkeypatch.setattr(os, "environ", {KEY: "literal $HOME value"})

    assert credentials.main(["--path", str(path)]) == 0

    captured = capfdbinary.readouterr()
    assert captured.err == b""
    assert captured.out.split(b"\0") == [
        b"literal $HOME value",
        str(tmp_path / ".stack-bds-password").encode(),
        b"",
    ]


def test_cli_executes_the_requested_command_with_decoded_credentials(tmp_path, monkeypatch):
    path = tmp_path / "secrets.env"
    monkeypatch.setattr(os, "environ", {KEY: "literal secret", "PATH": "/bin"})
    executed = []
    monkeypatch.setattr(os, "execvpe", lambda *args: executed.append(args))

    assert credentials.main(["--path", str(path), "--", "consumer", "literal $HOME"]) == 0

    executable, arguments, environment = executed.pop()
    assert executable == "consumer"
    assert arguments == ["consumer", "literal $HOME"]
    assert environment == {
        KEY: "literal secret",
        "PATH": "/bin",
        "XIAN_BDS_PASSWORD_FILE": str(tmp_path / ".stack-bds-password"),
    }


def test_cli_requires_a_command_after_the_separator(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(os, "environ", {})
    assert credentials.main(["--path", str(tmp_path / "secrets.env"), "--"]) == 1
    assert "a command must follow --" in capsys.readouterr().err


def test_cli_reports_invalid_credentials_without_disclosing_values(tmp_path, monkeypatch, capsys):
    path = tmp_path / "secrets.env"
    path.write_text('# xian-stack-credentials-v1\nUNKNOWN="private-value"\n')
    monkeypatch.setattr(os, "environ", {})
    assert credentials.main(["--path", str(path)]) == 1
    error = capsys.readouterr().err
    assert "invalid credential assignment" in error
    assert "private-value" not in error
