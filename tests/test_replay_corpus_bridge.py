from __future__ import annotations

import base64
import importlib.util
import io
import json
import os
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).absolute().parents[1]
spec = importlib.util.spec_from_file_location(
    "replay_corpus_bridge", ROOT / "scripts/replay_corpus_bridge.py"
)
bridge = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bridge
assert spec.loader is not None
spec.loader.exec_module(bridge)


def manifest():
    return {
        "height": 1,
        "app_hash": "a" * 64,
        "chain_id": "bridge-test",
        "expected_results": {"1": "b" * 64},
        "expected_app_hashes": {"1": "a" * 64},
        "repositories": {name: "1" * 40 for name in bridge.REPOSITORIES},
        "dirty_repositories": [],
        "image_id": "sha256:" + "2" * 64,
        "execution_policy": {
            "execution_revision": "xvm-execution-15",
            "gas_schedule": "xvm-gas-7",
            "bytecode_version": "xvm-2",
            "set_codec": "xian-set-1",
            "compiler_admission_revision": "xian-admission-3",
            "tx_admission_revision": "xian-fastpath-admission-3",
        },
    }


def public_files():
    files = {
        "home/config/config.toml": (
            b'[statesync]\nenable=false\n[p2p]\nseeds=""\npersistent_peers=""\npex=false\n'
        ),
        "home/config/xian.toml": (
            b'bds_enabled=false\n[bds]\npassword_file="/run/secrets/bds_password"\n'
        ),
        "home/config/genesis.json": json.dumps(
            {
                "chain_id": "bridge-test",
                "contracts": [{"code": "password = 'documentation only'\n"}],
            }
        ).encode(),
        "home/config/addrbook.json": b'{"addrs":[]}',
        "home/config/state-patches/test.json": b'{"patches":[]}',
    }
    for name in bridge.DB_NAMES:
        for file, data in {
            "CURRENT": b"MANIFEST-000001\n",
            "MANIFEST-000001": b"native-public-layout",
            "000001.ldb": b'ledger payload {"private_key":"immutable-on-chain-sensitive-value"}',
            "000002.log": b"native leveldb journal",
            "LOCK": b"",
        }.items():
            files[f"home/data/{name}/{file}"] = data
    return files


def archive_bytes(files=None, *, extra=None, directories=None):
    files = public_files() if files is None else files
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name in sorted(bridge.DIRECTORIES if directories is None else directories):
            member = tarfile.TarInfo(name)
            member.type = tarfile.DIRTYPE
            archive.addfile(member)
        for name, data in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        if extra:
            member, data = extra
            archive.addfile(member, io.BytesIO(data) if data is not None else None)
    return output.getvalue()


def write_plain(directory, *, data=None, body=None):
    directory.mkdir()
    (directory / "blocks.tar.gz").write_bytes(archive_bytes() if data is None else data)
    (directory / "manifest.json").write_bytes(
        json.dumps(manifest() if body is None else body).encode()
    )
    return directory


def git(directory, *args):
    return subprocess.run(
        [
            "git",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "user.name=Replay Bridge Test",
            "-c",
            "user.email=replay@example.test",
            "-C",
            str(directory),
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def commit_pair(directory):
    git(directory, "init", "-q")
    git(directory, "add", ".")
    git(directory, "commit", "-qm", "temporary fixture")
    return git(directory, "rev-parse", "HEAD")


def environment(directory, ref="3" * 40):
    return {
        "REPLAY_CORPUS_REF": ref,
        "REPLAY_ARCHIVE_SHA256": bridge.digest((directory / "blocks.tar.gz.enc").read_bytes()),
        "REPLAY_MANIFEST_SHA256": bridge.digest((directory / "manifest.json").read_bytes()),
    }


@pytest.fixture
def pair(tmp_path):
    plain = write_plain(tmp_path / "plain")
    encrypted, key = tmp_path / "encrypted", tmp_path / "private.key"
    summary = bridge.encrypt_pair(plain, encrypted, key)
    ref = commit_pair(encrypted)
    return plain, encrypted, key, environment(encrypted, ref), summary


def seal_without_validation(directory, archive, raw_manifest, key):
    directory.mkdir()
    header = bridge.canonical_json(
        {
            "schema_version": 1,
            "archive_sha256": bridge.digest(archive),
            "archive_bytes": len(archive),
            "manifest_sha256": bridge.digest(raw_manifest),
        }
    )
    prefix = bridge.MAGIC + struct.pack(">I", len(header)) + header
    data = prefix + bytes(bridge.aead_class()(key).encrypt(archive, prefix + raw_manifest))
    (directory / "blocks.tar.gz.enc").write_bytes(data)
    (directory / "manifest.json").write_bytes(raw_manifest)
    return environment(directory) | {"REPLAY_TRANSPORT_KEY": base64.b64encode(key).decode()}


def test_input_modes():
    assert bridge.parse_inputs({}).mode == "normal"
    assert bridge.parse_inputs({"REPLAY_RUN_ID": "123"}).mode == "prior"
    value = {
        "REPLAY_CORPUS_REF": "1" * 40,
        "REPLAY_ARCHIVE_SHA256": "2" * 64,
        "REPLAY_MANIFEST_SHA256": "3" * 64,
    }
    assert bridge.parse_inputs(value).mode == "ref"


@pytest.mark.parametrize("value", ["0", "-1", "01", "1.0", " 1", "+1", "1\n"])
def test_prior_run_must_be_canonical_positive_integer(value):
    with pytest.raises(ValueError):
        bridge.parse_inputs({"REPLAY_RUN_ID": value})


@pytest.mark.parametrize(
    "change",
    [
        {"REPLAY_CORPUS_REF": "branch"},
        {"REPLAY_CORPUS_REF": "A" * 40},
        {"REPLAY_ARCHIVE_SHA256": "Z" * 64},
        {"REPLAY_MANIFEST_SHA256": "short"},
        {"REPLAY_RUN_ID": "123"},
        {"REPLAY_MANIFEST_SHA256": ""},
    ],
)
def test_ref_inputs_are_complete_immutable_and_exclusive(change):
    values = {
        "REPLAY_CORPUS_REF": "1" * 40,
        "REPLAY_ARCHIVE_SHA256": "2" * 64,
        "REPLAY_MANIFEST_SHA256": "3" * 64,
    }
    values.update(change)
    with pytest.raises(ValueError):
        bridge.parse_inputs(values)


def test_end_to_end_ciphertext_import_and_private_authenticated_decrypt(
    pair, tmp_path, monkeypatch
):
    plain, encrypted, key, env, summary = pair
    copied, proof = tmp_path / "copied", tmp_path / "import-proof.json"
    monkeypatch.setattr(bridge, "aead_class", lambda: pytest.fail("keyless import called crypto"))
    imported = bridge.import_pair(
        encrypted, copied, proof, env | {"REPLAY_TRANSPORT_KEY": "unused-invalid-secret"}
    )
    assert imported["ciphertext_only"] is True and imported["decryption_required"] is True
    assert {p.name for p in copied.iterdir()} == bridge.PUBLIC_PAIR
    assert (copied / "blocks.tar.gz.enc").read_bytes() == (
        encrypted / "blocks.tar.gz.enc"
    ).read_bytes()
    monkeypatch.undo()
    output, decrypted_proof = tmp_path / "private-output", tmp_path / "decrypt-proof.json"
    decoded = bridge.decrypt_pair(
        copied, output, decrypted_proof, env | {"REPLAY_TRANSPORT_KEY": key.read_text()}
    )
    assert decoded["authenticated"] is True
    assert {p.name for p in output.iterdir()} == bridge.PLAIN_PAIR
    for name in bridge.PLAIN_PAIR:
        assert (output / name).read_bytes() == (plain / name).read_bytes()
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in output.iterdir())
    assert (
        key.read_text().strip()
        not in proof.read_text() + decrypted_proof.read_text() + json.dumps(summary)
    )
    assert b"immutable-on-chain-sensitive-value" not in (copied / "blocks.tar.gz.enc").read_bytes()


@pytest.mark.parametrize("name", ["REPLAY_ARCHIVE_SHA256", "REPLAY_MANIFEST_SHA256"])
def test_both_digests_are_checked_before_any_json_parse(pair, tmp_path, monkeypatch, name):
    _, encrypted, _, env, _ = pair
    monkeypatch.setattr(
        bridge, "json_object", lambda _: pytest.fail("parsed before SHA validation")
    )
    with pytest.raises(ValueError, match="SHA256"):
        bridge.import_pair(encrypted, tmp_path / "out", tmp_path / "proof", env | {name: "0" * 64})


@pytest.mark.parametrize(
    "case",
    [
        "wrongref",
        "tracked",
        "untracked",
        "directory",
        "symlink",
        "executable",
        "changed",
        "assumed-unchanged",
    ],
)
def test_git_ref_and_worktree_cannot_smuggle_other_data(pair, tmp_path, case):
    _, directory, _, env, _ = pair
    if case == "wrongref":
        env["REPLAY_CORPUS_REF"] = "0" * 40
    elif case in {"tracked", "untracked"}:
        (directory / "network.json").write_text('{"private_key":"do-not-copy"}')
        if case == "tracked":
            git(directory, "add", ".")
            git(directory, "commit", "-qm", "extra tracked")
            env["REPLAY_CORPUS_REF"] = git(directory, "rev-parse", "HEAD")
    elif case == "directory":
        (directory / "unexpected-home").mkdir()
    elif case == "symlink":
        path = directory / "manifest.json"
        outside = tmp_path / "outside.json"
        path.rename(outside)
        path.symlink_to(outside)
    elif case == "executable":
        os.chmod(directory / "manifest.json", 0o755)
        git(directory, "add", ".")
        git(directory, "commit", "-qm", "executable data")
        env["REPLAY_CORPUS_REF"] = git(directory, "rev-parse", "HEAD")
    else:
        path = directory / "blocks.tar.gz.enc"
        if case == "assumed-unchanged":
            git(directory, "update-index", "--assume-unchanged", path.name)
        data = bytearray(path.read_bytes())
        data[-1] ^= 1
        path.write_bytes(data)
        env["REPLAY_ARCHIVE_SHA256"] = bridge.digest(data)
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        bridge.import_pair(directory, tmp_path / "out", tmp_path / "proof", env)
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("case", ["tag", "nonce", "wrongkey", "header", "manifest-aad"])
def test_aead_authenticates_nonce_ciphertext_header_and_exact_manifest(pair, tmp_path, case):
    _, directory, key, env, _ = pair
    copied = tmp_path / "cipher-copy"
    shutil.copytree(directory, copied, ignore=shutil.ignore_patterns(".git"))
    path = copied / "blocks.tar.gz.enc"
    data = bytearray(path.read_bytes())
    env["REPLAY_TRANSPORT_KEY"] = key.read_text()
    prefix = len(bridge.MAGIC) + 4
    size = struct.unpack(">I", data[len(bridge.MAGIC) : prefix])[0]
    end = prefix + size
    if case == "wrongkey":
        env["REPLAY_TRANSPORT_KEY"] = base64.b64encode(os.urandom(32)).decode()
    elif case == "nonce":
        data[end] ^= 1
    elif case == "tag":
        data[-1] ^= 1
    else:
        header = json.loads(data[prefix:end])
        if case == "header":
            header["archive_sha256"] = "0" * 64
        else:
            raw = (copied / "manifest.json").read_bytes() + b"\n"
            (copied / "manifest.json").write_bytes(raw)
            header["manifest_sha256"] = bridge.digest(raw)
            env["REPLAY_MANIFEST_SHA256"] = bridge.digest(raw)
        data = bytearray(
            bridge.MAGIC
            + struct.pack(">I", len(bridge.canonical_json(header)))
            + bridge.canonical_json(header)
            + data[end:]
        )
    path.write_bytes(data)
    env["REPLAY_ARCHIVE_SHA256"] = bridge.digest(data)
    with pytest.raises(ValueError, match="authentication"):
        bridge.decrypt_pair(copied, tmp_path / "out", tmp_path / "proof", env)
    assert not (tmp_path / "out").exists() and not (tmp_path / "proof").exists()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.update(height=True),
        lambda m: m["repositories"].pop("xian-py"),
        lambda m: m["repositories"].update({"xian-py": "main"}),
        lambda m: m.update(dirty_repositories=["xian-py"]),
        lambda m: m["expected_results"].update({"01": "b" * 64}),
        lambda m: m["expected_app_hashes"].update({"1": "0" * 64}),
        lambda m: m.update(image_id="mutable:latest"),
        lambda m: m["execution_policy"].update(gas_schedule="unknown"),
        lambda m: m.update(private_key="unrelated-private-field"),
    ],
)
def test_manifest_requires_clean_exact_canonical_replay_provenance(mutate):
    value = manifest()
    mutate(value)
    with pytest.raises(ValueError):
        bridge.validate_manifest(value)


def audit_record():
    return {
        "schema_version": 1,
        "original_manifest_sha256": "4" * 64,
        "original_dirty_repositories": ["xian-contracting", "xian-py"],
        "excluded_untracked_environment_links": {
            "xian-contracting": [".venv", "packages/xian-zk/.venv"],
            "xian-py": [".venv"],
        },
        "tracked_source_unchanged": True,
        "archive_unchanged": True,
        "expected_results_unchanged": True,
        "expected_app_hashes_unchanged": True,
        "image_source_proof_sha256": "5" * 64,
    }


def test_audit_attestation_is_public_bounded_and_never_overrides_dirty_flag():
    value = manifest()
    value["source_environment_audit"] = audit_record()
    bridge.validate_manifest(value)
    value["dirty_repositories"] = ["xian-py"]
    with pytest.raises(ValueError, match="Dirty"):
        bridge.validate_manifest(value)
    value["dirty_repositories"] = []
    value["source_environment_audit"]["excluded_untracked_environment_links"]["xian-py"] = [
        "/private/target/.venv"
    ]
    with pytest.raises(ValueError, match="relative"):
        bridge.validate_manifest(value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("archive_unchanged", False),
        ("image_source_proof_sha256", "wrong"),
    ],
)
def test_audit_rejects_false_claims_or_bad_types(field, value):
    record = audit_record()
    record[field] = value
    with pytest.raises(ValueError):
        bridge.validate_audit(record)


@pytest.mark.parametrize(
    "name",
    [
        "home/config/priv_validator_key.json",
        "home/config/node_key.json",
        "home/data/priv_validator_state.json",
        "home/data/cs.wal/wal",
        "home/xian/state/data.mdb",
        "home/operator-bundle.json",
        "home/data/state.db/private.key",
        "../outside",
        "/absolute",
        "home/config/../node_key.json",
    ],
)
def test_tar_rejects_identity_state_wal_extra_paths_and_traversal(name):
    files = public_files() | {name: b"forbidden"}
    with pytest.raises((ValueError, tarfile.TarError)):
        bridge.validate_archive(archive_bytes(files), manifest(), bridge.Limits())


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE])
def test_tar_rejects_links_and_special_members(kind):
    member = tarfile.TarInfo("home/config/linked.json")
    member.type = kind
    member.linkname = "genesis.json" if kind != tarfile.FIFOTYPE else ""
    with pytest.raises(ValueError):
        bridge.validate_archive(archive_bytes(extra=(member, None)), manifest(), bridge.Limits())


def test_tar_rejects_duplicate_and_trailing_payload():
    member = tarfile.TarInfo("home/config/genesis.json")
    member.size = 2
    with pytest.raises(ValueError, match="Duplicate"):
        bridge.validate_archive(archive_bytes(extra=(member, b"{}")), manifest(), bridge.Limits())
    # A concatenated authenticated gzip member cannot hide data after tar EOF.
    import gzip

    with pytest.raises(ValueError, match="trailing"):
        bridge.validate_archive(
            archive_bytes() + gzip.compress(b"private trailing payload"),
            manifest(),
            bridge.Limits(),
        )


@pytest.mark.parametrize(
    "name,data",
    [
        ("home/config/xian.toml", b'bds_enabled=false\npassword="literal-secret"\n'),
        ("home/config/state-patches/test.json", b'{"private_key":"literal-secret"}'),
        ("home/config/addrbook.json", b'{"endpoint":"https://user:password@example.test"}'),
    ],
)
def test_public_config_rejects_literal_credentials_but_not_contract_source(name, data):
    with pytest.raises(ValueError, match="credential"):
        bridge.validate_archive(
            archive_bytes(public_files() | {name: data}), manifest(), bridge.Limits()
        )


def test_authenticated_but_unsafe_tar_is_rejected_after_decrypt(tmp_path):
    key = os.urandom(32)
    data = archive_bytes(public_files() | {"home/config/node_key.json": b"secret"})
    pair = tmp_path / "pair"
    env = seal_without_validation(pair, data, json.dumps(manifest()).encode(), key)
    with pytest.raises(ValueError, match="forbidden"):
        bridge.decrypt_pair(pair, tmp_path / "out", tmp_path / "proof", env)
    assert not (tmp_path / "out").exists()


def test_encrypt_and_decrypt_never_overwrite_or_place_key_proof_in_artifact(pair, tmp_path):
    plain, encrypted, key, env, _ = pair
    original = key.read_bytes()
    with pytest.raises(ValueError, match="already exist"):
        bridge.encrypt_pair(plain, tmp_path / "new", key)
    assert key.read_bytes() == original and not (tmp_path / "new").exists()
    with pytest.raises(ValueError, match="outside"):
        bridge.encrypt_pair(plain, tmp_path / "new", tmp_path / "new/key")
    with pytest.raises(ValueError, match="outside"):
        bridge.import_pair(encrypted, tmp_path / "out", tmp_path / "out/proof.json", env)
    copied = tmp_path / "copied"
    bridge.import_pair(encrypted, copied, tmp_path / "proof", env)
    with pytest.raises(ValueError, match="already exist"):
        bridge.import_pair(encrypted, copied, tmp_path / "proof2", env)


@pytest.mark.parametrize("key", ["", "not-base64", base64.b64encode(b"short").decode(), "A" * 128])
def test_transport_key_is_required_only_for_decrypt(key):
    with pytest.raises(ValueError):
        bridge.transport_key({"REPLAY_TRANSPORT_KEY": key})


@pytest.mark.parametrize("case", ["magic", "hugeheader", "zeroheader", "length"])
def test_envelope_framing_is_strict(pair, case):
    _, directory, _, _, _ = pair
    data = (directory / "blocks.tar.gz.enc").read_bytes()
    raw = (directory / "manifest.json").read_bytes()
    if case == "magic":
        data = b"invalid" + data
    elif case in {"hugeheader", "zeroheader"}:
        data = (
            bridge.MAGIC
            + struct.pack(">I", 4097 if case == "hugeheader" else 0)
            + data[len(bridge.MAGIC) + 4 :]
        )
    else:
        data = data[:-1]
    with pytest.raises(ValueError):
        bridge.parse_envelope(data, raw, bridge.Limits())


def test_limits_and_duplicate_json_fail_closed(pair, tmp_path):
    plain, encrypted, _, env, _ = pair
    with pytest.raises(ValueError, match="size limit"):
        bridge.encrypt_pair(plain, tmp_path / "new", tmp_path / "new.key", bridge.Limits(1, 100000))
    with pytest.raises(ValueError, match="size limit"):
        bridge.import_pair(
            encrypted, tmp_path / "out", tmp_path / "proof", env, bridge.Limits(1, 1)
        )
    with pytest.raises(ValueError, match="Duplicate"):
        bridge.json_object(b'{"a":1,"a":2}')


def test_cli_validation_is_bounded_and_does_not_echo_key(monkeypatch, capsys):
    for name in (
        "REPLAY_RUN_ID",
        "REPLAY_CORPUS_REF",
        "REPLAY_ARCHIVE_SHA256",
        "REPLAY_MANIFEST_SHA256",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("REPLAY_TRANSPORT_KEY", "do-not-print")
    assert bridge.main(["validate-inputs"]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "normal"
    monkeypatch.setenv("REPLAY_RUN_ID", "0")
    assert bridge.main(["validate-inputs"]) == 1
    assert "do-not-print" not in str(capsys.readouterr())


def test_actual_private_corpus_encrypted_git_import_fixture(tmp_path):
    path = os.environ.get("XIAN_REPLAY_BRIDGE_ACTUAL_CORPUS")
    if not path:
        pytest.skip("private local corpus is supplied only in the explicit integration gate")
    source = Path(path)
    encrypted, key = tmp_path / "encrypted", tmp_path / "key"
    private_output = tmp_path / "plain"
    try:
        result = bridge.encrypt_pair(source, encrypted, key)
        ref = commit_pair(encrypted)
        env = environment(encrypted, ref)
        copied = tmp_path / "copied"
        bridge.import_pair(encrypted, copied, tmp_path / "import-proof", env)
        bridge.decrypt_pair(
            copied,
            private_output,
            tmp_path / "decrypt-proof",
            env | {"REPLAY_TRANSPORT_KEY": key.read_text()},
        )
        for name in bridge.PLAIN_PAIR:
            if (private_output / name).read_bytes() != (source / name).read_bytes():
                pytest.fail("Private replay output differs from source; raw payload is not logged")
        assert result["height"] == 9195
    finally:
        shutil.rmtree(private_output, ignore_errors=True)
        key.unlink(missing_ok=True)


def test_malformed_audit_members_reject_with_boundary_error():
    audit = audit_record()
    audit["original_dirty_repositories"] = [["not-a-repository-string"]]
    with pytest.raises(ValueError):
        bridge.validate_audit(audit)


def test_malformed_public_config_tables_reject_with_boundary_error():
    files = public_files() | {"home/config/config.toml": b'statesync="not-a-table"\n'}
    with pytest.raises(ValueError):
        bridge.validate_archive(archive_bytes(files), manifest(), bridge.Limits())


def test_regular_tar_members_cannot_disguise_directory_paths():
    files = public_files()
    files["home/data/state.db/CURRENT/"] = files.pop("home/data/state.db/CURRENT")
    with pytest.raises(ValueError):
        bridge.validate_archive(archive_bytes(files), manifest(), bridge.Limits())


def test_giant_pax_metadata_is_refused_before_large_decompressor_read(monkeypatch):
    import gzip

    header = tarfile.TarInfo("././@PaxHeader")
    header.type = tarfile.XHDTYPE
    header.size = 1 << 30
    compressed = gzip.compress(header.tobuf(format=tarfile.USTAR_FORMAT) + b"\0" * 1024)
    original = gzip.GzipFile.read

    def guarded_read(stream, size=-1):
        if size > 65536:
            pytest.fail(f"tar requested unsafe metadata read of {size}bytes before rejection")
        return original(stream, size)

    monkeypatch.setattr(gzip.GzipFile, "read", guarded_read)
    with pytest.raises(ValueError, match="metadata"):
        bridge.validate_archive(compressed, manifest(), bridge.Limits())


def test_large_public_config_is_read_only_after_all_metadata_validates():
    files = public_files()
    files["home/config/genesis.json"] = json.dumps(
        {"chain_id": "bridge-test", "contracts": [{"code": "#" * 100000}]}
    ).encode()
    assert bridge.validate_archive(archive_bytes(files), manifest(), bridge.Limits()) > 1


@pytest.mark.parametrize(
    "kind", [tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK, tarfile.XGLTYPE]
)
def test_giant_other_auxiliary_headers_are_bounded_before_allocation(monkeypatch, kind):
    import gzip

    header = tarfile.TarInfo("././@LongLink")
    header.type = kind
    header.size = 1 << 30
    compressed = gzip.compress(header.tobuf(format=tarfile.USTAR_FORMAT) + b"\0" * 1024)
    original = gzip.GzipFile.read

    def guarded_read(stream, size=-1):
        if size > 65536:
            pytest.fail("unsafe auxiliary metadata allocation reached gzip")
        return original(stream, size)

    monkeypatch.setattr(gzip.GzipFile, "read", guarded_read)
    with pytest.raises(ValueError, match="metadata"):
        bridge.validate_archive(compressed, manifest(), bridge.Limits())


def test_valid_small_corpus_respects_64k_manifest_override(tmp_path):
    plain = write_plain(tmp_path / "plain")
    limits = bridge.Limits(max_manifest_bytes=65536)
    result = bridge.encrypt_pair(plain, tmp_path / "encrypted", tmp_path / "key", limits)
    assert result["height"] == 1


def test_tar_rejects_unlisted_empty_directory():
    member = tarfile.TarInfo("home/unrelated-wallet-home")
    member.type = tarfile.DIRTYPE
    with pytest.raises(ValueError, match="forbidden"):
        bridge.validate_archive(archive_bytes(extra=(member, None)), manifest(), bridge.Limits())


def test_tar_rejects_directory_with_nonzero_payload():
    member = tarfile.TarInfo("home/config/state-patches")
    member.type = tarfile.DIRTYPE
    member.size = 1
    directories = bridge.DIRECTORIES - {member.name}
    with pytest.raises(ValueError, match="forbidden"):
        bridge.validate_archive(
            archive_bytes(extra=(member, b"x"), directories=directories),
            manifest(),
            bridge.Limits(),
        )


def test_metadata_reader_uses_actual_bytes_for_its_cumulative_budget():
    reader = bridge.MetadataReader(io.BytesIO(b"a" * 65536 + b"b"), 1)
    assert reader.read(65536) == b"a" * 65536
    with pytest.raises(ValueError, match="budget"):
        reader.read(1)
    with pytest.raises(ValueError, match="read exceeds"):
        bridge.MetadataReader(io.BytesIO(b""), 1).read(65537)


def test_expanded_member_budget_charges_header_and_padded_payload():
    # Exercise accounting directly: the full archive also independently checks its
    # uncompressed tail. A one-byte body occupies a complete 512-byte tar record.
    member = tarfile.TarInfo("home/data/state.db/000010.ldb")
    member.size = 1
    with pytest.raises(ValueError, match="expanded limits"):
        bridge.checked_members([member], bridge.Limits(max_archive_bytes=15))
    with pytest.raises(ValueError, match="required public"):
        bridge.checked_members([member], bridge.Limits(max_archive_bytes=16))


def envelope_bytes(*, archive_size=1, schema_version=1):
    raw_manifest = json.dumps(manifest()).encode()
    header = {
        "schema_version": schema_version,
        "archive_sha256": "a" * 64,
        "archive_bytes": archive_size,
        "manifest_sha256": bridge.digest(raw_manifest),
    }
    raw_header = bridge.canonical_json(header)
    prefix = bridge.MAGIC + struct.pack(">I", len(raw_header)) + raw_header
    return prefix + bytes(int(archive_size) + bridge.AEAD_OVERHEAD), raw_manifest


@pytest.mark.parametrize("schema", [True, 2, 1.0])
def test_envelope_schema_requires_exact_integer_revision(schema):
    data, raw = envelope_bytes(schema_version=schema)
    with pytest.raises(ValueError, match="schema"):
        bridge.parse_envelope(data, raw, bridge.Limits())


@pytest.mark.parametrize("size", [True, 1.0, 0])
def test_envelope_archive_size_requires_positive_exact_integer(size):
    data, raw = envelope_bytes(archive_size=size)
    with pytest.raises(ValueError, match="archive size"):
        bridge.parse_envelope(data, raw, bridge.Limits())


def test_envelope_accepts_exact_one_byte_operator_cap():
    data, raw = envelope_bytes()
    header, message, aad = bridge.parse_envelope(data, raw, bridge.Limits(1, 65536))
    assert header["archive_bytes"] == 1
    assert len(message) == 1 + bridge.AEAD_OVERHEAD
    assert aad.endswith(raw)


@pytest.mark.parametrize("case", ["magic", "zero", "oversize", "truncated"])
def test_envelope_rejects_invalid_framing_before_json_parse(monkeypatch, case):
    data, raw = envelope_bytes()
    prefix = len(bridge.MAGIC) + 4
    if case == "magic":
        data = b"!" + data[1:]
    else:
        size = {"zero": 0, "oversize": 4097, "truncated": len(data)}[case]
        data = bridge.MAGIC + struct.pack(">I", size) + data[prefix:]

    def no_json_before_framing(*_):
        pytest.fail("Unbounded or malformed envelope reached JSON parsing")

    monkeypatch.setattr(bridge, "json_object", no_json_before_framing)
    with pytest.raises(ValueError, match="magic|header length"):
        bridge.parse_envelope(data, raw, bridge.Limits())


@pytest.mark.parametrize("case", ["backslash", "linkname"])
def test_native_member_metadata_rejects_invalid_path_and_link_fields(case):
    member = tarfile.TarInfo("home/config/genesis.json")
    if case == "backslash":
        member.name = "home/config\\genesis.json"
    else:
        member.linkname = "home/config/node_key.json"
    with pytest.raises(ValueError, match="path|links"):
        bridge.member_name(member)


def test_canonical_directory_slash_and_full_public_pax_set_are_accepted():
    directory = tarfile.TarInfo("home/config/")
    directory.type = tarfile.DIRTYPE
    assert bridge.member_name(directory) == "home/config"
    member = tarfile.TarInfo("home/config/genesis.json")
    member.pax_headers = {"path": member.name, "mtime": "0", "atime": "0", "ctime": "0"}
    assert bridge.member_name(member) == member.name


def test_metadata_zero_read_does_not_consume_budget_or_stream():
    reader = bridge.MetadataReader(io.BytesIO(b"x"), 1)
    assert reader.read(0) == b""
    assert reader.read(1) == b"x"


def native_members(count):
    required = sorted(bridge.REQUIRED_CONFIG) + [
        f"home/data/{name}/CURRENT" for name in ("blockstore.db", "state.db", "evidence.db")
    ]
    for name in required:
        yield tarfile.TarInfo(name)
    for number in range(count - len(required)):
        yield tarfile.TarInfo(f"home/data/state.db/{number:06}.ldb")


def test_native_archive_member_limit_accepts_exact_boundary_and_refuses_next():
    assert len(bridge.checked_members(native_members(100000), bridge.Limits())) == 100000
    with pytest.raises(ValueError, match="expanded limits"):
        bridge.checked_members(native_members(100001), bridge.Limits())


def test_authenticated_keyholder_packet_still_requires_correct_inner_digest(tmp_path):
    key = os.urandom(32)
    archive = archive_bytes()
    raw = json.dumps(manifest()).encode()
    header = bridge.canonical_json(
        {
            "schema_version": 1,
            "archive_sha256": "0" * 64,
            "archive_bytes": len(archive),
            "manifest_sha256": bridge.digest(raw),
        }
    )
    prefix = bridge.MAGIC + struct.pack(">I", len(header)) + header
    data = prefix + bytes(bridge.aead_class()(key).encrypt(archive, prefix + raw))
    encrypted = tmp_path / "encrypted"
    encrypted.mkdir()
    (encrypted / "blocks.tar.gz.enc").write_bytes(data)
    (encrypted / "manifest.json").write_bytes(raw)
    env = environment(encrypted) | {"REPLAY_TRANSPORT_KEY": base64.b64encode(key).decode()}
    with pytest.raises(ValueError, match="inner archive digest/length"):
        bridge.decrypt_pair(encrypted, tmp_path / "out", tmp_path / "proof", env)
    assert not (tmp_path / "out").exists()
