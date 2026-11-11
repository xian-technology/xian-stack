"""Launcher passes decoded credential data through a correctly encoded URI."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "docker/postgraphile/start-postgraphile.sh"


@pytest.fixture
def launch_graphile(tmp_path):
    real_node = shutil.which("node")
    assert real_node is not None
    log = tmp_path / "graphile.json"
    fake_node = tmp_path / "node"
    fake_node.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "assert sys.argv[1] == '/usr/src/app/connection-string.mjs'\n"
        "command = [os.environ['REAL_NODE'], os.environ['CONNECTION_HELPER']]\n"
        "os.execv(command[0], command)\n"
    )
    fake_node.chmod(0o700)
    graphile = tmp_path / "postgraphile"
    graphile.write_text(
        f"#!{sys.executable}\n"
        "import os, sys, json\n"
        "with open(os.environ['GRAPHILE_LOG'], 'w') as handle:\n"
        "    json.dump(sys.argv[1:], handle)\n"
    )
    graphile.chmod(0o700)

    def run(**values):
        log.unlink(missing_ok=True)
        environment = {
            **os.environ,
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "REAL_NODE": real_node,
            "CONNECTION_HELPER": str(ROOT / "docker/postgraphile/connection-string.mjs"),
            "GRAPHILE_LOG": str(log),
            "POSTGRAPHILE_CONNECTION": "",
            "POSTGRAPHILE_SCHEMA_WAIT_TIMEOUT_SECONDS": "0",
            "PGUSER": "user/#?@name",
            "PGPASSWORD": "placeholder",
            "PGDATABASE": "db/name?#",
            "PGPORT": "5432",
            **values,
        }
        result = subprocess.run(
            ["sh", str(LAUNCHER)], env=environment, capture_output=True, text=True, check=False
        )
        arguments = json.loads(log.read_text()) if log.exists() else None
        return result, arguments

    return run


@pytest.mark.parametrize(
    "password",
    [
        "placeholder",
        "apostrophe's quote\"",
        "literal-$HOME-`text`-$(text)",
        "x#/@?:%y",
        "line1\nline2\n",
    ],
)
def test_launcher_preserves_password_in_encoded_connection(launch_graphile, password):
    result, arguments = launch_graphile(PGPASSWORD=password)
    assert result.returncode == 0, result.stderr
    connection = arguments[arguments.index("-c") + 1]
    parsed = urlsplit(connection)
    assert unquote(parsed.username) == "user/#?@name"
    assert unquote(parsed.password) == password
    assert unquote(parsed.path[1:]) == "db/name?#"
    assert parsed.hostname == "postgres"
    assert parsed.port == 5432
    assert result.stdout == ""
    assert password not in result.stderr


def test_launcher_preserves_explicit_connection(launch_graphile):
    connection = "postgres://user:explicit%40secret@custom-db:5433/other"
    result, arguments = launch_graphile(POSTGRAPHILE_CONNECTION=connection, PGPORT="invalid")
    assert result.returncode == 0, result.stderr
    assert arguments[arguments.index("-c") + 1] == connection


@pytest.mark.parametrize("port", ["1", "65535"])
def test_connection_port_includes_both_valid_boundaries(launch_graphile, port):
    result, arguments = launch_graphile(PGPORT=port)
    assert result.returncode == 0, result.stderr
    assert urlsplit(arguments[arguments.index("-c") + 1]).port == int(port)


@pytest.mark.parametrize("port", ["invalid", "0", "65536", ""])
def test_connection_build_failure_stops_before_launch(launch_graphile, port):
    result, arguments = launch_graphile(PGPORT=port, PGPASSWORD="private-placeholder")
    assert result.returncode != 0
    assert arguments is None
    assert "private-placeholder" not in result.stderr


def test_connection_helper_has_literal_defaults():
    helper = ROOT / "docker/postgraphile/connection-string.mjs"
    result = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            f"import {{connectionString}} from {json.dumps(helper.as_uri())};"
            "process.stdout.write(connectionString({}));",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "postgres://xian_graphql:@postgres:5432/xian"
