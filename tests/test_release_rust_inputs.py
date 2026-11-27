import hashlib
import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).absolute().parents[1] / "scripts"))

import release_rust_inputs as inputs


@pytest.fixture
def frozen(tmp_path):
    source = tmp_path / "contracting"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=test",
            "-c",
            "user.email=t@t",
            "commit",
            "--allow-empty",
            "-qm",
            "frozen",
        ],
        check=True,
    )
    ref = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    root = tmp_path / "locks"
    selected = root / ref
    selected.mkdir(parents=True)
    hashes = {}
    for crate in inputs.RUST_WHEELS:
        (source / "packages" / crate).mkdir(parents=True)
        data = f"# {crate}\nversion = 4\n".encode()
        (selected / f"{crate}.lock").write_bytes(data)
        hashes[crate] = hashlib.sha256(data).hexdigest()
    metadata = {"contracting_ref": ref, "sha256": hashes}
    (selected / "inputs.json").write_text(json.dumps(metadata))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"components": {"xian-contracting": {"ref": ref}}}))
    return manifest, source, root, selected, metadata


def test_restores_all_reviewed_locks_and_can_be_repeated(frozen):
    manifest, source, root, selected, metadata = frozen
    for _ in range(2):
        assert inputs.restore(manifest, source, root) == metadata
        for crate in inputs.RUST_WHEELS:
            assert (source / "packages" / crate / "Cargo.lock").read_bytes() == (
                selected / f"{crate}.lock"
            ).read_bytes()


@pytest.mark.parametrize("damage", ["ref", "missing", "extra", "hash"])
def test_rejects_invalid_lock_sets_before_writing_anything(frozen, damage):
    manifest, source, root, selected, metadata = frozen
    if damage == "ref":
        metadata["contracting_ref"] = "0" * 40
    elif damage == "missing":
        metadata["sha256"].pop(inputs.RUST_WHEELS[-1])
    elif damage == "extra":
        metadata["sha256"]["../other"] = "bad"
    else:
        (selected / f"{inputs.RUST_WHEELS[-1]}.lock").write_text("changed")
    (selected / "inputs.json").write_text(json.dumps(metadata))
    with pytest.raises(ValueError):
        inputs.restore(manifest, source, root)
    assert not list(source.rglob("Cargo.lock"))


def test_unknown_source_cannot_choose_a_lock_set_path(frozen):
    manifest, source, root, *_ = frozen
    manifest.write_text(json.dumps({"components": {"xian-contracting": {"ref": "../other"}}}))
    with pytest.raises(ValueError, match="immutable"):
        inputs.restore(manifest, source, root)


def test_cannot_seed_a_different_checkout(frozen):
    manifest, source, root, *_ = frozen
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=test",
            "-c",
            "user.email=t@t",
            "commit",
            "--allow-empty",
            "-qm",
            "new",
        ],
        check=True,
    )
    with pytest.raises(ValueError, match="checkout"):
        inputs.restore(manifest, source, root)


def test_cannot_seed_modified_sources(frozen):
    manifest, source, root, *_ = frozen
    tracked = source / "source.rs"
    tracked.write_text("original")
    subprocess.run(["git", "-C", str(source), "add", "source.rs"], check=True)
    with pytest.raises(ValueError, match="modified"):
        inputs.restore(manifest, source, root)


def test_existing_different_lock_is_not_silently_replaced(frozen):
    manifest, source, root, *_ = frozen
    destination = source / "packages" / inputs.RUST_WHEELS[-1] / "Cargo.lock"
    destination.write_text("local lock")
    with pytest.raises(ValueError, match="existing"):
        inputs.restore(manifest, source, root)
    assert destination.read_text() == "local lock"
    assert len(list(source.rglob("Cargo.lock"))) == 1


def test_command_emits_the_restored_input_evidence(frozen, monkeypatch, capsys):
    manifest, source, root, _, metadata = frozen
    monkeypatch.setattr(
        sys,
        "argv",
        [
            inputs.__file__,
            "--manifest",
            str(manifest),
            "--contracting-root",
            str(source),
            "--inputs-root",
            str(root),
        ],
    )
    runpy.run_path(inputs.__file__, run_name="__main__")
    assert json.loads(capsys.readouterr().out) == metadata


def test_release_manifest_selects_packaged_rust_inputs(tmp_path):
    manifest = json.loads((inputs.INPUTS_ROOT.parent.parent / "release-manifest.json").read_text())
    ref = manifest["components"]["xian-contracting"]["ref"]
    selected = inputs.INPUTS_ROOT / ref
    metadata = json.loads((selected / "inputs.json").read_text())

    assert metadata["contracting_ref"] == ref
    assert len(inputs.lock_plan(tmp_path, selected, metadata)) == len(inputs.RUST_WHEELS)
