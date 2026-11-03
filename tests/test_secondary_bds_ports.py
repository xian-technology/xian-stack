"""Secondary indexer connections use Docker's actual loopback port allocation."""

import asyncio
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_localnet_parallel_phase import localnet_e2e as e2e


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

    monkeypatch.setattr(e2e, "run_cmd", command)
    monkeypatch.setattr(runner, "prepare_secondary_bds_home", lambda node: tmp_path)
    monkeypatch.setattr(e2e, "wait_for_container_state", AsyncMock(return_value="healthy"))
    node = SimpleNamespace(rpc_url="http://127.0.0.1:26657")
    first = runner.run_secondary_bds_reindex(source_node=node, reset=True)
    assert asyncio.run(runner.restart_secondary_bds_postgres()) == "healthy"
    second = runner.run_secondary_bds_reindex(source_node=node, reset=False)
    assert [first["host_port"], second["host_port"]] == [32123, 32124]
    for env, port in zip(reindexes, (32123, 32124), strict=True):
        assert env["XIAN_BDS_PORT"] == str(port)
        assert f"@127.0.0.1:{port}/" in env["XIAN_BDS_DSN"]


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
