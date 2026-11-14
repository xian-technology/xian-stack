"""Secondary indexer connections use Docker's actual loopback port allocation."""

import asyncio
import socket
import stat
import tomllib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_localnet_block_ordering_phase import localnet_e2e as e2e


@pytest.fixture
def runner(tmp_path):
    item = object.__new__(e2e.E2ERunner)
    item.run_id = "port-regression"
    item.output_dir = tmp_path
    item.args = SimpleNamespace(port_offset=1000)
    return item


def test_start_works_while_former_fixed_port_is_occupied(runner, monkeypatch):
    with socket.socket() as occupied, socket.socket() as published:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        fixed_port = occupied.getsockname()[1]
        runner.args.port_offset = fixed_port - 55432

        def command(args, **kwargs):
            if args[1] == "run":
                binding = args[args.index("--publish") + 1]
                parts = binding.split(":")
                if len(parts) == 2:
                    host, port = "0.0.0.0", int(parts[0])
                else:
                    host, port = parts[0], int(parts[1] or 0)
                published.bind((host, port))
                published.listen()
            elif args[1] == "port":
                host, port = published.getsockname()
                return SimpleNamespace(stdout=f"{host}:{port}\n")
            return SimpleNamespace(stdout="", stderr="")

        monkeypatch.setattr(e2e, "run_cmd", command)
        monkeypatch.setattr(e2e.subprocess, "run", lambda *args, **kwargs: None)
        monkeypatch.setattr(e2e, "wait_for_container_state", AsyncMock(return_value="healthy"))
        result = asyncio.run(runner.start_secondary_bds_postgres())
        assert result["host_port"] == published.getsockname()[1] != fixed_port
        assert published.getsockname()[0] == "127.0.0.1"
        assert result["state"] == "healthy"


def test_reindex_rediscovers_changed_mapping_after_restart(runner, monkeypatch, tmp_path):
    mapped_port = 32123
    reindexes = []

    def command(args, **kwargs):
        nonlocal mapped_port
        if args[:2] == ["docker", "port"]:
            assert args[2:] == [runner.secondary_bds_container_name(), "5432/tcp"]
            return SimpleNamespace(stdout=f"127.0.0.1:{mapped_port}\n")
        if args[:2] == ["docker", "start"]:
            mapped_port = 32124
        if args[0] == "uv":
            reindexes.append(kwargs["env"])
        return SimpleNamespace(stdout="", stderr="")

    xian_toml = tmp_path / ".cometbft" / "config" / "xian.toml"
    xian_toml.parent.mkdir(parents=True)
    xian_toml.write_text(
        'bds_enabled = true\n\n[bds]\nhost = "localnet-postgres"\nport = 5432\n'
        'password_file = "/run/secrets/xian_bds_password"\n'
    )
    observed_bds = []

    def record(args, **kwargs):
        if args[0] == "uv":
            observed_bds.append(tomllib.loads(xian_toml.read_text())["bds"])
        return command(args, **kwargs)

    monkeypatch.setattr(e2e, "run_cmd", record)
    monkeypatch.setattr(runner, "prepare_secondary_bds_home", lambda node: tmp_path)
    monkeypatch.setattr(e2e, "wait_for_container_state", AsyncMock(return_value="healthy"))
    monkeypatch.setenv("XIAN_BDS_ENABLED", "1")
    node = SimpleNamespace(rpc_url="http://127.0.0.1:26657")
    first = runner.run_secondary_bds_reindex(source_node=node, reset=True)
    assert asyncio.run(runner.restart_secondary_bds_postgres()) == "healthy"
    second = runner.run_secondary_bds_reindex(source_node=node, reset=False)
    assert [first["host_port"], second["host_port"]] == [32123, 32124]
    password_file = runner.secondary_bds_password_file()
    for env, bds, port in zip(reindexes, observed_bds, (32123, 32124), strict=True):
        # The reindexer reads its settings only from xian.toml, its password
        # only from the file the secondary Postgres was initialised from.
        assert [key for key in env if key.startswith("XIAN_BDS_")] == ["XIAN_BDS_ENABLED"]
        assert (bds["host"], bds["port"], bds["user"], bds["database"]) == (
            "127.0.0.1",
            port,
            "xian",
            "xian",
        )
        assert bds["password_file"] == str(password_file)
        assert "password" not in bds and "dsn" not in bds


def test_secondary_postgres_reads_its_password_from_an_owner_only_file(runner, monkeypatch):
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        if args[:2] == ["docker", "port"]:
            return SimpleNamespace(stdout="127.0.0.1:32123\n")
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(e2e, "run_cmd", command)
    monkeypatch.setattr(e2e.subprocess, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(e2e, "wait_for_container_state", AsyncMock(return_value="healthy"))
    asyncio.run(runner.start_secondary_bds_postgres())

    password_file = runner.secondary_bds_password_file()
    assert stat.S_IMODE(password_file.stat().st_mode) == 0o600
    password = password_file.read_text()
    assert len(password) >= 32
    docker_run = next(call for call in calls if call[:2] == ["docker", "run"])
    assert f"{password_file}:/run/secrets/xian_bds_password:ro" in docker_run
    assert "POSTGRES_PASSWORD_FILE=/run/secrets/xian_bds_password" in docker_run
    assert not [arg for arg in docker_run if arg.startswith("POSTGRES_PASSWORD=")]
    assert password not in " ".join(docker_run)


@pytest.mark.parametrize(
    "binding",
    [
        "",
        "0.0.0.0:32123",
        "[::]:32123",
        "127.0.0.1:0",
        "127.0.0.1:65536",
        "127.0.0.1:32123\n127.0.0.1:32124",
        "127.0.0.1:not-a-port",
    ],
)
def test_missing_unsafe_or_ambiguous_binding_fails_closed(runner, monkeypatch, binding):
    monkeypatch.setattr(e2e, "run_cmd", lambda *args, **kwargs: SimpleNamespace(stdout=binding))
    with pytest.raises(e2e.E2EError, match="secondary BDS.*binding"):
        runner.secondary_bds_host_port()


@pytest.mark.parametrize("port", [1, 65535])
def test_valid_binding_port_boundaries(runner, monkeypatch, port):
    monkeypatch.setattr(
        e2e, "run_cmd", lambda *args, **kwargs: SimpleNamespace(stdout=f"127.0.0.1:{port}\n")
    )
    assert runner.secondary_bds_host_port() == port


@pytest.mark.parametrize(
    "text",
    ["bds_enabled = true\n", '[bds]\nhost = "a"\n[other]\nkey = 1\n'],
)
def test_bds_settings_need_the_trailing_bds_table(tmp_path, text):
    xian_toml = tmp_path / "xian.toml"
    xian_toml.write_text("x = 1\n" + text)

    with pytest.raises(e2e.E2EError, match="last table"):
        e2e.write_bds_table(xian_toml, {"port": 1})
    assert xian_toml.read_text() == "x = 1\n" + text
