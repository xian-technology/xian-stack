import gzip
import json
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).absolute().parents[1] / "scripts"))
import export_node_store_snapshot as exporter


def home(tmp_path):
    root = tmp_path / "home"
    (root / "config").mkdir(parents=True)
    (root / "config/config.toml").write_text(
        'priv_validator_state_file="data/custom-signing.json"\nnode_key_file="xian/custom-node-key.json"\n'
    )
    for relative in [
        "data/state.db/CURRENT",
        "data/evidence.db/CURRENT",
        "data/blockstore.db/CURRENT",
        "xian/lmdb/data.mdb",
        "data/custom-signing.json",
        "data/priv_validator_state.json",
        "xian/custom-node-key.json",
        "xian/logs/log",
        "xian/snapshots/old.tar",
        "data/cs.wal/wal",
        "config/priv_validator_key.json",
    ]:
        p = root / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(relative)
    return root


def test_export_selection_has_store_data_but_no_identity_signing_logs_or_nested_backups(tmp_path):
    root = home(tmp_path)
    paths = exporter.store_files(root)
    assert set(paths) == {
        "data/state.db/CURRENT",
        "data/evidence.db/CURRENT",
        "data/blockstore.db/CURRENT",
        "xian/lmdb/data.mdb",
    }


def test_source_symlink_rejects_before_copy(tmp_path):
    root = home(tmp_path)
    (root / "data/linked-key").symlink_to(root / "config/priv_validator_key.json")
    with pytest.raises(ValueError, match="symlink"):
        exporter.store_files(root)


def test_archive_is_exact_counted_tar_stream_and_keeps_only_selected_files(tmp_path):
    root = home(tmp_path)
    out = tmp_path / "store.tar.gz"
    from types import SimpleNamespace

    sizes = exporter.write_archive(
        root,
        exporter.store_files(root),
        out,
        SimpleNamespace(
            max_archive_bytes=1000000, max_uncompressed_bytes=1000000, min_free_disk_bytes=0
        ),
    )
    raw = gzip.decompress(out.read_bytes())
    assert sizes == {"snapshot_bytes": len(out.read_bytes()), "uncompressed_bytes": len(raw)}
    with tarfile.open(out) as archive:
        assert set(archive.getnames()) == set(exporter.store_files(root))
        assert all(
            member.isfile() and member.uid == member.gid == member.mtime == 0 for member in archive
        )


def test_output_publication_rolls_back_own_archive_if_manifest_exists(tmp_path):
    archive = tmp_path / "tmp.tar"
    archive.write_text("archive")
    manifest = tmp_path / "tmp.json"
    manifest.write_text("manifest")
    final_archive = tmp_path / "final.tar"
    final_manifest = tmp_path / "final.json"
    final_manifest.write_text("operator-owned")
    with pytest.raises(FileExistsError):
        exporter.publish_outputs(archive, manifest, final_archive, final_manifest)
    assert not final_archive.exists()
    assert final_manifest.read_text() == "operator-owned"


def test_changed_source_file_is_not_a_coherent_stopped_snapshot(tmp_path):
    root = home(tmp_path)
    files = exporter.store_files(root)
    before = exporter.source_fingerprint(root, files)
    (root / "xian/lmdb/data.mdb").write_text("new committed state")
    with pytest.raises(ValueError, match="changed"):
        exporter.require_unchanged_source(root, files, before)


def test_publication_only_links_destination_local_staged_files(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    archive = work / "archive"
    archive.write_bytes(b"public store")
    manifest = work / "manifest"
    manifest.write_bytes(b"public signed manifest")
    original = exporter.os.link

    def destination_local(source, target):
        assert source.parent == target.parent, "cross-mount hardlink would fail EXDEV"
        original(source, target)

    monkeypatch.setattr(exporter.os, "link", destination_local)
    exporter.publish_outputs(archive, manifest, out / "store.tar.gz", other / "store.json")
    assert (out / "store.tar.gz").read_bytes() == b"public store"
    assert (other / "store.json").read_bytes() == b"public signed manifest"
    assert sorted(p.name for p in out.iterdir()) == ["store.tar.gz"]
    assert sorted(p.name for p in other.iterdir()) == ["store.json"]


def test_manifest_directory_sync_failure_never_leaves_manifest_without_archive(
    tmp_path, monkeypatch
):
    work = tmp_path / "work"
    work.mkdir()
    a = tmp_path / "a"
    a.mkdir()
    m = tmp_path / "m"
    m.mkdir()
    source_a = work / "a"
    source_a.write_text("a")
    source_m = work / "m"
    source_m.write_text("m")

    def sync(directory):
        if directory == m:
            raise OSError("directory fsync failed")

    monkeypatch.setattr(exporter, "sync_directory", sync)
    with pytest.raises(OSError, match="fsync"):
        exporter.publish_outputs(source_a, source_m, a / "archive", m / "manifest")
    assert (a / "archive").read_text() == "a"
    assert (m / "manifest").read_text() == "m"


@pytest.mark.parametrize(
    "field,maximum,label",
    [("max_archive_bytes", 20, "compressed"), ("max_uncompressed_bytes", 512, "uncompressed")],
)
def test_archive_limits_stop_writing_before_oversized_output(tmp_path, field, maximum, label):
    from types import SimpleNamespace

    root = home(tmp_path)
    values = {
        "max_archive_bytes": 1000000,
        "max_uncompressed_bytes": 1000000,
        "min_free_disk_bytes": 0,
    }
    values[field] = maximum
    with pytest.raises(ValueError, match=label):
        exporter.write_archive(
            root, exporter.store_files(root), tmp_path / "store.tar.gz", SimpleNamespace(**values)
        )
    if field == "max_archive_bytes":
        assert (tmp_path / "store.tar.gz").stat().st_size <= maximum


def test_export_signs_canonical_v2_and_verifies_real_app_checkpoint(tmp_path, monkeypatch):
    """Real LMDB/root/signature path; native parsing has independent Go fixtures."""
    import contextlib
    import importlib.util
    from types import SimpleNamespace

    import xian.full_node_store as protocol

    fixture_path = (
        Path(__file__).resolve().parents[2] / "xian-abci/tests/unit/full_node_store_fixtures.py"
    )
    spec = importlib.util.spec_from_file_location("node_store_interop_fixture", fixture_path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    source = tmp_path / "source"
    fixture.reference_config(source)
    native = fixture.seed_application(source / "xian")
    (source / "data").mkdir()
    (source / "data/native-checkpoint.json").write_text(json.dumps(native))
    monkeypatch.setattr(protocol, "inspect_native_store", fixture.fixture_inspector)

    @contextlib.contextmanager
    def lease(*args):
        yield native

    monkeypatch.setattr(exporter, "native_lease", lease)
    key = tmp_path / "publisher.key"
    key.write_text("17" * 32)
    key.chmod(0o600)
    work = tmp_path / "work"
    work.mkdir()
    archive = tmp_path / "published.tar.gz"
    document = tmp_path / "published.json"
    args = SimpleNamespace(
        home=source,
        work=work,
        archive_output=archive,
        manifest_output=document,
        signing_key_file=key,
        snapshot_url=archive.as_uri(),
        offline=True,
    )
    result = exporter.export_snapshot(args)
    manifest = json.loads(document.read_text())
    assert result["manifest"] == manifest
    assert manifest["height"] == 123 and manifest["app_hash"] == native["app_hash"]
    assert manifest["snapshot_sha256"] == exporter.sha256_file(archive)
    assert manifest["uncompressed_bytes"] == len(gzip.decompress(archive.read_bytes()))
    checked = protocol.verify_full_node_store_manifest(
        manifest,
        trusted_manifest_public_keys=[manifest["signing_public_key"]],
        expected_chain_id="xian-test-1",
    )
    assert checked == manifest
    assert key.read_text() not in json.dumps(result)
    with tarfile.open(archive) as stream:
        assert all(
            not item.name.startswith("config/") and not item.name.endswith("lock.mdb")
            for item in stream
        )
    assert list(work.iterdir()) == []


def test_donor_incomplete_restore_marker_blocks_actual_export_before_copy(tmp_path, monkeypatch):
    from types import SimpleNamespace

    source = home(tmp_path)
    marker = source / ".snapshot-restore-interrupted"
    marker.mkdir()
    (marker / "publication-started").touch()
    key = tmp_path / "key"
    key.write_text("a" * 64)
    key.chmod(0o600)
    work = tmp_path / "work"
    work.mkdir()

    def no_copy(*args):
        raise AssertionError("native lease/copy must not begin for incomplete donor")

    monkeypatch.setattr(exporter, "native_lease", no_copy)
    args = SimpleNamespace(
        home=source,
        work=work,
        archive_output=tmp_path / "a.tar.gz",
        manifest_output=tmp_path / "a.json",
        signing_key_file=key,
        snapshot_url="https://example.test/a.tar.gz",
        offline=True,
    )
    with pytest.raises(ValueError, match="incomplete snapshot restore"):
        exporter.export_snapshot(args)
    assert not args.archive_output.exists() and not args.manifest_output.exists()
    assert (marker / "publication-started").is_file()


@pytest.mark.parametrize(
    "raw,expected", [('{"height":"7"}\n', {"height": "7"}), ("", None), ("x" * 65537, None)]
)
def test_lease_payload_is_bounded_without_reading_live_stderr(monkeypatch, raw, expected):
    import io
    from types import SimpleNamespace

    process = SimpleNamespace(stdout=io.StringIO(raw))
    monkeypatch.setattr(exporter.select, "select", lambda *args: ([process.stdout], [], []))
    if expected is None:
        with pytest.raises(ValueError, match="invalid output"):
            exporter.lease_payload(process, 3)
    else:
        assert exporter.lease_payload(process, 3) == expected


def test_lease_payload_timeout_is_explicit(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(exporter.select, "select", lambda *args: ([], [], []))
    with pytest.raises(ValueError, match="timed out"):
        exporter.lease_payload(SimpleNamespace(stdout=None), 2)


def test_native_lease_uses_central_limits_and_reference_genesis_then_closes_every_pipe(
    tmp_path, monkeypatch
):
    import io
    from types import SimpleNamespace

    calls = []
    process = SimpleNamespace(
        stdin=io.StringIO(),
        stdout=io.StringIO('{"height":"7"}\n'),
        stderr=io.StringIO(),
        poll=lambda: None,
        wait=lambda **kwargs: 0,
    )
    monkeypatch.setattr(
        exporter.subprocess, "Popen", lambda command, **kwargs: calls.append(command) or process
    )
    monkeypatch.setattr(exporter.select, "select", lambda *args: ([process.stdout], [], []))
    monkeypatch.setenv("XIAN_NODE_STORE_INSPECT_MAX_HISTORY_HEIGHTS", "2345678")
    monkeypatch.setenv("XIAN_NODE_STORE_INSPECT_TIMEOUT_SECONDS", "456")
    with exporter.native_lease(tmp_path / "data", "goleveldb", tmp_path / "genesis.json") as result:
        assert result == {"height": "7"} and not process.stdin.closed
    assert process.stdin.closed and process.stdout.closed and process.stderr.closed
    assert calls[0][calls[0].index("--max-history-heights") + 1] == "2345678"
    assert calls[0][calls[0].index("--timeout") + 1] == "456s"
    assert calls[0][calls[0].index("--genesis-file") + 1] == str(tmp_path / "genesis.json")


def test_known_home_aliases_exclude_arbitrarily_named_absolute_identity_files(tmp_path):
    root = home(tmp_path)
    alias = tmp_path / "known-alias"
    alias.symlink_to(root, target_is_directory=True)
    paths = {
        "priv_validator_state_file": root / "data/arbitrary-signing.secret",
        "node_key_file": alias / "xian/arbitrary-node.secret",
        "priv_validator_key_file": alias / "data/arbitrary-validator.secret",
    }
    (root / "config/config.toml").write_text(
        "\n".join(key + "=" + json.dumps(str(value)) for key, value in paths.items())
    )
    for value in paths.values():
        value.write_text("PRIVATE_EXPORT_SENTINEL")
    selected = exporter.store_files(root, aliases=[alias])
    assert all("arbitrary-" not in name for name in selected)
    assert not any("PRIVATE_EXPORT_SENTINEL" in (root / name).read_text() for name in selected)


def test_unrelated_source_alias_cannot_weaken_identity_exclusions(tmp_path):
    root = home(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(ValueError, match="alias"):
        exporter.store_files(root, aliases=[other])


def test_source_root_symlink_and_missing_root_are_rejected(tmp_path):
    root = home(tmp_path)
    (root / "config/config.toml").write_text("")
    outside = tmp_path / "outside"
    (root / "xian").rename(outside)
    (root / "xian").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="plain"):
        exporter.store_files(root)
    (root / "xian").unlink()
    with pytest.raises(ValueError, match="plain"):
        exporter.store_files(root)


def test_sized_writer_accepts_exact_caps_and_keeps_the_whole_disk_reserve(tmp_path, monkeypatch):
    import io
    from types import SimpleNamespace

    target = io.BytesIO()
    writer = exporter.SizedWriter(target, 3, "archive", disk_directory=tmp_path, reserve=7)
    monkeypatch.setattr(exporter.shutil, "disk_usage", lambda path: SimpleNamespace(free=10))
    assert writer.write(b"abc") == 3 and target.getvalue() == b"abc"
    with pytest.raises(ValueError, match="limit"):
        writer.write(b"d")
    target = io.BytesIO()
    writer = exporter.SizedWriter(target, 100, "archive", disk_directory=tmp_path, reserve=7)
    monkeypatch.setattr(exporter.shutil, "disk_usage", lambda path: SimpleNamespace(free=9))
    with pytest.raises(ValueError, match="disk"):
        writer.write(b"abc")
    assert target.getvalue() == b""


def test_gzip_timestamp_is_deterministic(tmp_path):
    from types import SimpleNamespace

    root = home(tmp_path)
    target = tmp_path / "a.tar.gz"
    exporter.write_archive(
        root,
        exporter.store_files(root),
        target,
        SimpleNamespace(
            max_archive_bytes=1000000, max_uncompressed_bytes=1000000, min_free_disk_bytes=0
        ),
    )
    assert target.read_bytes()[4:8] == b"\0" * 4


def test_source_genesis_missing_file_is_rejected_at_resolution(tmp_path):
    root = home(tmp_path)
    with pytest.raises(FileNotFoundError):
        exporter.source_genesis(root)


@pytest.mark.parametrize("mismatch", ["height", "app_hash"])
def test_locked_native_and_staged_checkpoint_must_match_each_dimension(
    tmp_path, monkeypatch, mismatch
):
    import contextlib

    import xian.node_admin as protocol

    root = home(tmp_path)
    (root / "config/genesis.json").write_text("{}")
    native = {"height": "123", "app_hash": "a" * 64}
    point = {"height": 123, "app_hash": "a" * 64}
    point[mismatch] = 124 if mismatch == "height" else "b" * 64

    @contextlib.contextmanager
    def lease(*args):
        yield native

    monkeypatch.setattr(exporter, "native_lease", lease)
    monkeypatch.setattr(protocol, "read_full_node_store_checkpoint", lambda *args, **kwargs: point)
    staged = tmp_path / "staged"
    staged.mkdir()
    with pytest.raises(ValueError, match="locked source"):
        exporter.copy_stopped_store(root, staged, exporter.store_files(root))


@pytest.mark.parametrize("failure", [None, "members", "payload", "disk"])
def test_export_preflight_checks_exact_member_payload_and_full_disk_reserve(
    tmp_path, monkeypatch, failure
):
    from types import SimpleNamespace

    import xian.full_node_store as protocol

    source = home(tmp_path)
    files = exporter.store_files(source)
    total = sum((source / name).stat().st_size for name in files)
    limits = SimpleNamespace(
        max_members=len(files), max_uncompressed_bytes=total, min_free_disk_bytes=123
    )
    free = 3 * total + limits.min_free_disk_bytes
    if failure == "members":
        limits.max_members -= 1
    if failure == "payload":
        limits.max_uncompressed_bytes -= 1
    if failure == "disk":
        free -= 1
    monkeypatch.setattr(protocol, "FullNodeStoreLimits", lambda: limits)
    monkeypatch.setattr(exporter.shutil, "disk_usage", lambda directory: SimpleNamespace(free=free))
    key = tmp_path / "key"
    key.write_text("17" * 32)
    key.chmod(0o600)
    args = SimpleNamespace(
        home=source,
        work=tmp_path,
        archive_output=tmp_path / "out.tar.gz",
        manifest_output=tmp_path / "out.json",
        signing_key_file=key,
        offline=True,
    )
    if failure is None:
        _wallet, actual, selected = exporter.export_inputs(args)
        assert actual is limits and selected == files
    else:
        with pytest.raises(ValueError, match="too many|uncompressed limit|insufficient disk"):
            exporter.export_inputs(args)
    assert not args.archive_output.exists() and not args.manifest_output.exists()


def test_unavailable_absolute_identity_path_cannot_be_silently_archived_under_a_new_namespace(
    tmp_path,
):
    root = home(tmp_path)
    (root / "config/config.toml").write_text(
        'priv_validator_key_file="/unmounted-donor-home/data/arbitrary.secret"\n'
    )
    (root / "data/arbitrary.secret").write_text("PRIVATE_EXPORT_SENTINEL")
    with pytest.raises(ValueError, match="absolute identity path"):
        exporter.store_files(root)


def test_native_lease_accepts_exact_output_budget(monkeypatch):
    import io
    from types import SimpleNamespace

    raw = '{"height":"7"}' + (" " * (65536 - len('{"height":"7"}')))
    process = SimpleNamespace(stdout=io.StringIO(raw))
    monkeypatch.setattr(exporter.select, "select", lambda *args: ([process.stdout], [], []))
    assert exporter.lease_payload(process, 1) == {"height": "7"}


@pytest.mark.parametrize("kind", ["same", "existing", "broken"])
def test_output_preflight_rejects_alias_or_existing_target_before_signer_access(tmp_path, kind):
    from types import SimpleNamespace

    a = tmp_path / "archive"
    m = tmp_path / "manifest"
    if kind == "same":
        m = a
    elif kind == "existing":
        a.write_text("operator file")
    else:
        a.symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="differ|exists"):
        exporter.require_output_paths(
            SimpleNamespace(offline=True, archive_output=a, manifest_output=m)
        )
