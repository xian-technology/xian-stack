import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).absolute().parents[1] / "scripts"))
import node_store_snapshot_backend as snapshot


def test_inspect_uses_selected_image_readonly_and_networkless(tmp_path, monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout='{"schema_version":1}', returncode=0)

    monkeypatch.setattr(snapshot.subprocess, "run", run)
    genesis = tmp_path / "genesis.json"
    genesis.write_text("{}")
    monkeypatch.setattr(
        snapshot,
        "inspection_limits",
        lambda: SimpleNamespace(
            timeout_seconds=120,
            command_arguments=lambda: ["--max-history-heights", "1000000", "--timeout", "120s"],
        ),
    )
    result = snapshot.inspect_store(tmp_path, "goleveldb", genesis, "review:current")
    assert result == {"schema_version": 1}
    assert calls[0][calls[0].index("--network") + 1] == "none"
    assert "review:current" in calls[0]
    assert any("target=/data,readonly" in token for token in calls[0])
    assert "--read-only" in calls[0]


def test_export_refuses_without_explicit_offline_ack_before_docker(tmp_path, monkeypatch):
    run = Mock()
    monkeypatch.setattr(snapshot.subprocess, "run", run)
    with pytest.raises(ValueError, match="offline"):
        snapshot.export_snapshot(
            home=tmp_path,
            archive_output=tmp_path / "a.tar.gz",
            manifest_output=tmp_path / "a.json",
            snapshot_url="https://example.test/a.tar.gz",
            signing_key_file=tmp_path / "key",
            image="current",
            offline=False,
        )
    run.assert_not_called()


@pytest.mark.parametrize("rw", [True, None])
def test_running_writable_home_mount_prevents_export(tmp_path, monkeypatch, rw):
    answers = [
        SimpleNamespace(stdout="abc\n"),
        SimpleNamespace(stdout=json.dumps([{"Mounts": [{"Source": str(tmp_path), "RW": rw}]}])),
    ]
    monkeypatch.setattr(snapshot.subprocess, "run", lambda *args, **kwargs: answers.pop(0))
    with pytest.raises(ValueError, match="running container"):
        snapshot.require_stopped_home(tmp_path)


def test_docker_query_failure_cannot_prove_offline(tmp_path, monkeypatch):
    def failure(*args, **kwargs):
        raise snapshot.subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(snapshot.subprocess, "run", failure)
    with pytest.raises(snapshot.subprocess.CalledProcessError):
        snapshot.require_stopped_home(tmp_path)


def test_known_no_running_containers_allows_offline_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(
        snapshot.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout="")
    )
    snapshot.require_stopped_home(tmp_path)


def test_structured_backend_accepts_export_and_inspect_actions(tmp_path):
    import backend
    from stack_backend.request import args_from_backend_request

    parser = backend.build_parser()
    common = {
        "node_image_mode": "registry",
        "node_integrated_image": "registry.invalid/node@sha256:" + "a" * 64,
    }
    export = args_from_backend_request(
        parser,
        {
            "schema_version": 1,
            "command": "node-store-snapshot-export",
            "options": {
                **common,
                "home": str(tmp_path),
                "archive_output": str(tmp_path / "a.tar.gz"),
                "manifest_output": str(tmp_path / "a.json"),
                "snapshot_url": "https://example.test/a.tar.gz",
                "signing_key_file": str(tmp_path / "key"),
                "offline": True,
            },
        },
    )
    assert export.offline is True
    inspect = args_from_backend_request(
        parser,
        {
            "schema_version": 1,
            "command": "node-store-snapshot-inspect",
            "options": {
                **common,
                "db_dir": str(tmp_path),
                "db_backend": "goleveldb",
                "genesis_file": str(tmp_path / "genesis.json"),
            },
        },
    )
    assert inspect.db_backend == "goleveldb"


def test_export_refuses_output_parent_alias_into_source_home(tmp_path, monkeypatch):
    source = tmp_path / "home"
    source.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(source, target_is_directory=True)
    key = tmp_path / "key"
    key.write_text("a" * 64)
    key.chmod(0o600)
    run = Mock()
    monkeypatch.setattr(snapshot.subprocess, "run", run)
    with pytest.raises(ValueError, match="outside the source home"):
        snapshot.export_snapshot(
            home=source,
            archive_output=alias / "store.tar.gz",
            manifest_output=tmp_path / "store.json",
            snapshot_url="https://example.test/store.tar.gz",
            signing_key_file=key,
            image="current",
            offline=True,
        )
    run.assert_not_called()


def test_export_signer_private_file_cannot_be_inside_exported_home(tmp_path, monkeypatch):
    source = tmp_path / "home"
    source.mkdir()
    key = source / "key"
    key.write_text("a" * 64)
    key.chmod(0o600)
    run = Mock()
    monkeypatch.setattr(snapshot.subprocess, "run", run)
    with pytest.raises(ValueError, match="outside the exported home"):
        snapshot.export_snapshot(
            home=source,
            archive_output=tmp_path / "store.tar.gz",
            manifest_output=tmp_path / "store.json",
            snapshot_url="https://example.test/store.tar.gz",
            signing_key_file=key,
            image="current",
            offline=True,
        )
    run.assert_not_called()


def test_selected_image_passes_profile_overrides_to_one_stack_default_owner(monkeypatch):
    calls = []
    monkeypatch.setattr(
        snapshot.subprocess,
        "run",
        lambda args, **kwargs: (
            calls.append((args, kwargs)) or SimpleNamespace(stdout="chosen-current\n")
        ),
    )
    result = snapshot.selected_image(
        node_image_mode="registry",
        node_integrated_image="integrated-current",
    )
    assert result == "chosen-current"
    assert calls[0][1]["env"]["XIAN_NODE_IMAGE_MODE"] == "registry"
    assert calls[0][1]["env"]["XIAN_NODE_INTEGRATED_IMAGE"] == "integrated-current"
    assert str(snapshot.STACK / "scripts/stack-env.sh") in calls[0][0]


def test_backend_dispatch_preserves_recipient_genesis_and_selected_image(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot, "selected_image", lambda **kwargs: "chosen-current")
    inspect = Mock(return_value={"checked": True})
    monkeypatch.setattr(snapshot, "inspect_store", inspect)
    args = SimpleNamespace(
        command="node-store-snapshot-inspect",
        node_image_mode="registry",
        node_integrated_image="image",
        db_dir=tmp_path / "data",
        db_backend="goleveldb",
        genesis_file=tmp_path / "genesis.json",
    )
    assert snapshot.backend_snapshot(args) == {"checked": True}
    inspect.assert_called_once_with(args.db_dir, "goleveldb", args.genesis_file, "chosen-current")


def test_backend_export_real_dispatch_is_networkless_and_forwards_operator_budgets(
    tmp_path, monkeypatch
):
    source = tmp_path / "home"
    source.mkdir()
    key = tmp_path / "key"
    key.write_text("17" * 32)
    key.chmod(0o600)
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout="" if args[1] == "ps" else '{"ok":true}')

    monkeypatch.setattr(snapshot.subprocess, "run", run)
    monkeypatch.setenv("XIAN_NODE_STORE_INSPECT_MAX_HISTORY_HEIGHTS", "2345678")
    monkeypatch.setenv("XIAN_NODE_STORE_INSPECT_TIMEOUT_SECONDS", "456")
    result = snapshot.export_snapshot(
        home=source,
        archive_output=tmp_path / "archive.tar.gz",
        manifest_output=tmp_path / "manifest.json",
        snapshot_url="https://example.test/store.tar.gz",
        signing_key_file=key,
        image="chosen-current",
        offline=True,
    )
    assert result["ok"] is True
    command, options = calls[-1]
    assert command[command.index("--network") + 1] == "none"
    assert "chosen-current" in command and "--read-only" in command
    assert "XIAN_NODE_STORE_INSPECT_MAX_HISTORY_HEIGHTS=2345678" in command
    assert "XIAN_NODE_STORE_INSPECT_TIMEOUT_SECONDS=456" in command
    assert options["timeout"] > 456
    assert key.read_text() not in " ".join(command)
    assert not list(tmp_path.glob(".node-store-export-*"))


def test_export_request_without_offline_never_resolves_or_opens_runtime(tmp_path, monkeypatch):
    def forbidden(**kwargs):
        raise AssertionError("runtime selection must not precede offline acknowledgement")

    monkeypatch.setattr(snapshot, "selected_image", forbidden)
    args = SimpleNamespace(command="node-store-snapshot-export", offline=False)
    with pytest.raises(ValueError, match="offline"):
        snapshot.backend_snapshot(args)


def test_backend_export_dispatch_forwards_explicit_offline_and_all_destinations(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(snapshot, "selected_image", lambda **kwargs: "chosen-current")
    export = Mock(return_value={"ok": True})
    monkeypatch.setattr(snapshot, "export_snapshot", export)
    args = SimpleNamespace(
        command="node-store-snapshot-export",
        offline=True,
        node_image_mode="registry",
        node_integrated_image="current",
        home=tmp_path / "home",
        archive_output=tmp_path / "archive",
        manifest_output=tmp_path / "manifest",
        snapshot_url="https://example.test/store",
        signing_key_file=tmp_path / "key",
    )
    assert snapshot.backend_snapshot(args) == {"ok": True}
    export.assert_called_once_with(
        home=args.home,
        archive_output=args.archive_output,
        manifest_output=args.manifest_output,
        snapshot_url=args.snapshot_url,
        signing_key_file=args.signing_key_file,
        image="chosen-current",
        offline=True,
    )


@pytest.mark.parametrize(
    "field", ["home", "archive_output", "manifest_output", "snapshot_url", "signing_key_file"]
)
def test_backend_export_rejects_every_missing_required_field(tmp_path, monkeypatch, field):
    monkeypatch.setattr(snapshot, "selected_image", lambda **kwargs: "chosen-current")
    args = SimpleNamespace(
        command="node-store-snapshot-export",
        offline=True,
        node_image_mode="registry",
        node_integrated_image="current",
        home=tmp_path / "home",
        archive_output=tmp_path / "archive",
        manifest_output=tmp_path / "manifest",
        snapshot_url="https://example.test/store",
        signing_key_file=tmp_path / "key",
    )
    setattr(args, field, None)
    with pytest.raises(ValueError, match=field):
        snapshot.backend_snapshot(args)


def test_docker_output_honors_real_process_errors_and_returns_text(tmp_path, monkeypatch):
    command = tmp_path / "docker"
    command.write_text(
        "#!/usr/bin/env python3\nimport sys\n"
        'print("public process result")\n'
        'raise SystemExit(7 if "fail" in sys.argv else 0)\n'
    )
    command.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + ":" + snapshot.os.environ["PATH"])
    assert snapshot.docker_output("ok") == "public process result\n"
    with pytest.raises(snapshot.subprocess.CalledProcessError):
        snapshot.docker_output("fail")


@pytest.mark.parametrize("relative", ["parent", "child"])
def test_any_overlapping_writable_source_mount_prevents_copy(tmp_path, monkeypatch, relative):
    home = tmp_path / "home"
    home.mkdir()
    source = tmp_path if relative == "parent" else home / "data"
    answers = [
        SimpleNamespace(stdout="abc\n"),
        SimpleNamespace(stdout=json.dumps([{"Mounts": [{"Source": str(source), "RW": True}]}])),
    ]
    monkeypatch.setattr(snapshot.subprocess, "run", lambda *args, **kwargs: answers.pop(0))
    with pytest.raises(ValueError, match="running container"):
        snapshot.require_stopped_home(home)


def test_readonly_overlap_and_unrelated_live_mounts_do_not_claim_a_writer(tmp_path, monkeypatch):
    answers = [
        SimpleNamespace(stdout="abc\n"),
        SimpleNamespace(
            stdout=json.dumps(
                [
                    {
                        "Mounts": [
                            {"Source": str(tmp_path), "RW": False},
                            {"Source": str(tmp_path / "unrelated"), "RW": True},
                        ]
                    }
                ]
            )
        ),
    ]
    monkeypatch.setattr(snapshot.subprocess, "run", lambda *args, **kwargs: answers.pop(0))
    snapshot.require_stopped_home(tmp_path / "home")


def test_snapshot_output_response_names_host_paths(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    key = tmp_path / "key"
    key.write_text("17" * 32)
    key.chmod(0o600)

    def output(*args, **kwargs):
        return (
            ""
            if args[0] == "ps"
            else json.dumps(
                {
                    "ok": True,
                    "archive_output": "/archive-output/store.tar.gz",
                    "manifest_output": "/manifest-output/store.json",
                }
            )
        )

    monkeypatch.setattr(snapshot, "docker_output", output)
    result = snapshot.export_snapshot(
        home=home,
        archive_output=tmp_path / "store.tar.gz",
        manifest_output=tmp_path / "store.json",
        snapshot_url="https://example.test/store.tar.gz",
        signing_key_file=key,
        image="current",
        offline=True,
    )
    assert result["archive_output"] == str(tmp_path / "store.tar.gz")
    assert result["manifest_output"] == str(tmp_path / "store.json")


def test_check_stopped_accepts_fresh_home_without_selecting_an_image(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot, "docker_output", lambda *args, **kwargs: "")

    def no_image(**kwargs):
        raise AssertionError("offline guard needs no image")

    monkeypatch.setattr(snapshot, "selected_image", no_image)
    home = tmp_path / "not-created/home"
    result = snapshot.backend_snapshot(
        SimpleNamespace(command="node-store-snapshot-check-stopped", home=home)
    )
    assert result == {"runtime_stopped": True, "home": str(home)}
    assert not home.exists()


def test_check_stopped_rejects_writable_parent_of_fresh_home(tmp_path, monkeypatch):
    answers = ["abc\n", json.dumps([{"Mounts": [{"Source": str(tmp_path), "RW": True}]}])]
    monkeypatch.setattr(snapshot, "docker_output", lambda *args, **kwargs: answers.pop(0))
    with pytest.raises(ValueError, match="running container"):
        snapshot.backend_snapshot(
            SimpleNamespace(
                command="node-store-snapshot-check-stopped", home=tmp_path / "not-created/home"
            )
        )


def test_source_mounts_cover_host_alias_and_external_public_genesis(tmp_path):
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    genesis = tmp_path / "public-genesis.json"
    genesis.write_text("{}")
    (home / "config/config.toml").write_text("genesis_file=" + json.dumps(str(genesis)))
    mounts = snapshot.source_mounts(home)
    assert snapshot.bind(home, "/root/.cometbft") in mounts
    assert snapshot.bind(home, str(home)) in mounts
    assert snapshot.bind(genesis, str(genesis)) in mounts
    assert all(value.endswith(",readonly") for value in mounts)


def test_canonical_runtime_home_never_creates_a_duplicate_bind():
    mounts = snapshot.source_mounts(Path("/root/.cometbft"))
    assert mounts == [snapshot.bind(Path("/root/.cometbft"), "/root/.cometbft")]


@pytest.mark.parametrize("kind", ["same", "existing", "broken", "missingparent"])
def test_host_output_preflight_refuses_every_invalid_destination(tmp_path, kind):
    home = tmp_path / "home"
    home.mkdir()
    a = tmp_path / "archive"
    m = tmp_path / "manifest"
    if kind == "same":
        m = a
    elif kind == "existing":
        a.write_text("operator file")
    elif kind == "broken":
        a.symlink_to(tmp_path / "missing")
    else:
        a = tmp_path / "missingparent/archive"
    with pytest.raises(ValueError, match="differ|exists|parent"):
        snapshot.check_outputs(home, a, m)


def test_export_request_missing_offline_is_rejected_as_missing_acknowledgement(tmp_path):
    args = SimpleNamespace(
        command="node-store-snapshot-export",
        home=tmp_path,
        archive_output=tmp_path / "a",
        manifest_output=tmp_path / "m",
        snapshot_url="https://example.test/a",
        signing_key_file=tmp_path / "key",
    )
    with pytest.raises(ValueError, match="offline"):
        snapshot.validate_snapshot_request(args)


def test_export_path_preflight_rejects_missing_source_before_docker(tmp_path):
    key = tmp_path / "key"
    key.write_text("17" * 32)
    key.chmod(0o600)
    with pytest.raises(FileNotFoundError):
        snapshot.export_paths(tmp_path / "missing", tmp_path / "a", tmp_path / "m", key, True)


@pytest.mark.parametrize("kind", ["public", "directory"])
def test_export_path_preflight_requires_private_regular_signer_file(tmp_path, kind):
    home = tmp_path / "home"
    home.mkdir()
    key = tmp_path / "key"
    if kind == "directory":
        key.mkdir(mode=0o700)
    else:
        key.write_text("17" * 32)
        key.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        snapshot.export_paths(home, tmp_path / "a", tmp_path / "m", key, True)


@pytest.mark.parametrize("kind", ["relative", "inside", "unavailable"])
def test_only_needed_available_external_genesis_gets_an_additional_mount(tmp_path, kind):
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    genesis = home / "config/genesis.json"
    genesis.write_text("{}")
    value = (
        "config/genesis.json"
        if kind == "relative"
        else str(genesis if kind == "inside" else tmp_path / "missing-genesis")
    )
    (home / "config/config.toml").write_text("genesis_file=" + json.dumps(value))
    assert snapshot.source_mounts(home) == [
        snapshot.bind(home, "/root/.cometbft"),
        snapshot.bind(home, str(home)),
    ]
