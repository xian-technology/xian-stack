"""Credential persistence and shell/bootstrap consumers treat values as data."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import stack_credentials as credentials

ROOT = Path(__file__).resolve().parents[1]
KEYS = ("XIAN_BDS_PASSWORD", "XIAN_POSTGRAPHILE_PASSWORD")
VALUES = [
    "simple-placeholder",
    'apostrophe\'s "quoted" value\\end',
    "literal-$(printf changed)-`printf changed`-$HOME",
    " first line\nsecond line\n",
]


def shell_credentials(path, **overrides):
    return subprocess.run(
        [
            "bash",
            "-c",
            "source ./scripts/stack-env.sh && ensure_stack_secrets_env && "
            "python3 -c 'import json,os; print(json.dumps({k:os.environ[k] for k in "
            '["XIAN_BDS_PASSWORD","XIAN_POSTGRAPHILE_PASSWORD"]}))\'',
        ],
        cwd=ROOT,
        env={**os.environ, "XIAN_STACK_SECRETS_ENV": str(path), **overrides},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("value", VALUES)
def test_backend_written_credentials_roundtrip_through_shell(tmp_path, value):
    path = tmp_path / "secrets.env"
    expected = {KEYS[0]: value, KEYS[1]: "different-placeholder"}
    credentials.write_credentials(path, expected)
    assert credentials.read_credentials(path) == expected
    result = shell_credentials(path, **{key: "" for key in KEYS})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == expected
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_legacy_assignments_are_literal_and_environment_overrides_win(tmp_path):
    path = tmp_path / "legacy.env"
    source = (
        "# generated legacy assignments\n"
        "XIAN_BDS_PASSWORD='legacy-placeholder'\n"
        "XIAN_POSTGRAPHILE_PASSWORD=$(printf changed)\n"
    )
    path.write_text(source)
    result = shell_credentials(
        path, XIAN_BDS_PASSWORD="explicit' value\n", XIAN_POSTGRAPHILE_PASSWORD=""
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        KEYS[0]: "explicit' value\n",
        KEYS[1]: "$(printf changed)",
    }
    assert path.read_text() == source


@pytest.mark.parametrize(
    "source",
    [
        "not-an-assignment\n",
        "UNKNOWN_SECRET=placeholder\n",
        'XIAN_BDS_PASSWORD="unterminated\n',
        "XIAN_BDS_PASSWORD='unterminated\n",
        "XIAN_BDS_PASSWORD=first\nXIAN_BDS_PASSWORD=second\n",
        '# xian-stack-credentials-v1\nXIAN_BDS_PASSWORD="bad\\u0000value"\n',
        '# xian-stack-credentials-v2\nXIAN_BDS_PASSWORD="placeholder"\n',
        '# legacy comment\n# xian-stack-credentials-v1\nXIAN_BDS_PASSWORD="placeholder"\n',
    ],
)
def test_malformed_credentials_fail_before_shell_consumer_continues(tmp_path, source):
    path = tmp_path / "invalid.env"
    path.write_text(source)
    with pytest.raises(ValueError):
        credentials.read_credentials(path)
    result = shell_credentials(path, **{key: "" for key in KEYS})
    assert result.returncode != 0
    assert result.stdout == ""
    assert path.read_text() == source


def test_failed_atomic_write_preserves_previous_credentials(tmp_path, monkeypatch):
    path = tmp_path / "secrets.env"
    original = {KEYS[0]: "previous", KEYS[1]: "other-previous"}
    credentials.write_credentials(path, original)

    def fail_replace(*_args):
        raise OSError("simulated replacement failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replacement failure"):
        credentials.write_credentials(path, {KEYS[0]: "new", KEYS[1]: "other-new"})
    assert credentials.read_credentials(path) == original
    assert set(tmp_path.iterdir()) <= {path, path.with_name(path.name + ".lock")}


def test_legacy_quoted_backslashes_are_not_reinterpreted_as_json(tmp_path):
    path = tmp_path / "legacy.env"
    source = 'XIAN_BDS_PASSWORD="literal\\n\\t\\\\value"\nXIAN_POSTGRAPHILE_PASSWORD=other\n'
    path.write_text(source)
    expected = {KEYS[0]: r"literal\n\t\\value", KEYS[1]: "other"}
    assert credentials.read_credentials(path) == expected
    result = shell_credentials(path, **{key: "" for key in KEYS})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == expected
    assert path.read_text() == source


def test_credential_symlink_is_rejected_without_touching_target(tmp_path):
    target = tmp_path / "target"
    target.write_text("untouched-placeholder")
    path = tmp_path / "secrets.env"
    path.symlink_to(target)
    with pytest.raises((ValueError, OSError)):
        credentials.read_credentials(path)
    with pytest.raises((ValueError, OSError)):
        credentials.write_credentials(path, {KEYS[0]: "new", KEYS[1]: "other-new"})
    assert target.read_text() == "untouched-placeholder"


def test_concurrent_first_use_generates_one_shared_credential_pair(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    path = tmp_path / "secrets.env"
    barrier = Barrier(8)

    def load():
        barrier.wait()
        return credentials.ensure_credentials(path, {})

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _index: load(), range(8)))
    assert all(value == results[0] for value in results)
    assert credentials.read_credentials(path) == results[0]
    assert results[0][KEYS[0]] != results[0][KEYS[1]]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_command_consumer_receives_literal_environment(tmp_path):
    path = tmp_path / "secrets.env"
    values = {KEYS[0]: VALUES[-1], KEYS[1]: VALUES[-2]}
    credentials.write_credentials(path, values)
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/stack_credentials.py"),
            "--path",
            str(path),
            "--",
            sys.executable,
            "-c",
            "import os,json; print(json.dumps({k:os.environ[k] for k in "
            '["XIAN_BDS_PASSWORD","XIAN_POSTGRAPHILE_PASSWORD"]}))',
        ],
        env={**os.environ, **{key: "" for key in KEYS}},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == values


def test_failed_file_inspection_closes_the_open_descriptor(tmp_path, monkeypatch):
    path = tmp_path / "secrets.env"
    path.write_text("XIAN_BDS_PASSWORD=placeholder\n")
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


def test_empty_legacy_values_remain_empty_until_generation(tmp_path):
    path = tmp_path / "secrets.env"
    path.write_text("XIAN_BDS_PASSWORD=''\nXIAN_POSTGRAPHILE_PASSWORD=other\n")
    assert credentials.read_credentials(path) == {KEYS[0]: "", KEYS[1]: "other"}
    values = credentials.ensure_credentials(path, {})
    assert values[KEYS[0]]
    assert values[KEYS[1]] == "other"
    assert credentials.read_credentials(path) == values


def test_invalid_assignment_reports_its_source_line(tmp_path):
    path = tmp_path / "secrets.env"
    path.write_text("# operator credential file\ninvalid assignment\n")
    with pytest.raises(ValueError, match="line 2"):
        credentials.read_credentials(path)


def test_nested_credential_directory_is_created_privately(tmp_path):
    path = tmp_path / "nested" / "private" / "secrets.env"
    values = {KEYS[0]: "placeholder", KEYS[1]: "other"}
    credentials.write_credentials(path, values)
    assert credentials.read_credentials(path) == values
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize(
    "compose_variable",
    [
        "STACK_COMPOSE",
        "ABCI_COMPOSE",
        "ABCI_BDS_COMPOSE",
        "ABCI_FIDELITY_COMPOSE",
        "ABCI_DEV_COMPOSE",
        "CONTRACTING_COMPOSE",
        "LOCALNET_COMPOSE",
    ],
)
def test_make_compose_families_receive_decoded_environment(tmp_path, compose_variable):
    path = tmp_path / "private store" / "quoted' credential file.env"
    values = {KEYS[0]: "quoted' value\n", KEYS[1]: "literal$HOME-`text`"}
    credentials.write_credentials(path, values)
    recorder = tmp_path / "record compose.py"
    recorder.write_text(
        "import os,json\n"
        "print(json.dumps({key:os.environ[key] for key in "
        "['XIAN_BDS_PASSWORD','XIAN_POSTGRAPHILE_PASSWORD']}))\n"
    )
    probe = tmp_path / "probe.mk"
    probe.write_text(f"credential-probe:\n\t@$({compose_variable}) config\n")
    result = subprocess.run(
        ["make", "--no-print-directory", "-f", "Makefile", "-f", str(probe), "credential-probe"],
        cwd=ROOT,
        env={
            **os.environ,
            "XIAN_STACK_SECRETS_ENV": str(path),
            "XIAN_BDS_PASSWORD": "",
            "XIAN_POSTGRAPHILE_PASSWORD": "",
            "DOCKER_COMPOSE": f"{sys.executable} '{recorder}'",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == values


def test_make_help_does_not_create_or_load_credentials(tmp_path):
    path = tmp_path / "secrets.env"
    result = subprocess.run(
        ["make", "--no-print-directory", "help"],
        cwd=ROOT,
        env={
            **os.environ,
            "XIAN_STACK_SECRETS_ENV": str(path),
            "XIAN_BDS_PASSWORD": "",
            "XIAN_POSTGRAPHILE_PASSWORD": "",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not path.exists()
    assert not path.with_name(path.name + ".lock").exists()
