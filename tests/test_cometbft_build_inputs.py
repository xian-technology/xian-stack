from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).absolute().parents[1] / "scripts"))
import release_manifest

ROOT = Path(__file__).absolute().parents[1]


@pytest.fixture
def patched_manifest(tmp_path):
    data = release_manifest.load_manifest(ROOT / "release-manifest.json")
    patch = tmp_path / "docker/cometbft-patches/0001-verified-evidence-history.patch"
    patch.parent.mkdir(parents=True)
    patch.write_text("reviewed history patch fixture\n")
    data["build"].update(
        cometbft_source_commit="0880b4d378f347ab16e54ec677ff50d803f37d62",
        cometbft_patch_path=str(patch.relative_to(tmp_path)),
        cometbft_patch_sha256=hashlib.sha256(patch.read_bytes()).hexdigest(),
        cometbft_history_revision="1",
    )
    manifest = tmp_path / "release-manifest.json"
    manifest.write_text(json.dumps(data))
    return manifest, data, patch


def test_release_validation_rejects_changed_history_patch(patched_manifest):
    path, _, patch = patched_manifest
    patch.write_text("unreviewed replacement\n")
    with pytest.raises(SystemExit, match="patch checksum"):
        release_manifest.command_validate(SimpleNamespace(manifest=path))


def test_release_validation_requires_patch_file(patched_manifest):
    path, _, patch = patched_manifest
    patch.unlink()
    with pytest.raises(SystemExit, match="patch.*regular file"):
        release_manifest.command_validate(SimpleNamespace(manifest=path))


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "../outside.patch",
        "/tmp/outside.patch",
        "docker/cometbft-patches/../outside.patch",
        "docker/cometbft-patches/not-a-patch.txt",
        "docker/other/history.patch",
    ],
)
def test_release_validation_rejects_unsafe_patch_path(patched_manifest, unsafe_path):
    path, data, _ = patched_manifest
    data["build"]["cometbft_patch_path"] = unsafe_path
    path.write_text(json.dumps(data))
    with pytest.raises(SystemExit, match="patch path"):
        release_manifest.command_validate(SimpleNamespace(manifest=path))


def test_release_validation_rejects_patch_symlink(patched_manifest, tmp_path):
    path, _, patch = patched_manifest
    outside = tmp_path / "replacement.patch"
    patch.rename(outside)
    patch.symlink_to(outside)
    with pytest.raises(SystemExit, match="patch.*regular file"):
        release_manifest.command_validate(SimpleNamespace(manifest=path))


@pytest.mark.parametrize(
    "field,value",
    [
        ("cometbft_patch_sha256", "not-a-hash"),
        ("cometbft_source_commit", "short"),
        ("cometbft_source_sha256", "short"),
        ("cometbft_version", "0.39.3"),
        ("cometbft_history_revision", ""),
        ("cometbft_history_revision", "2"),
    ],
)
def test_release_validation_requires_pinned_history_inputs(patched_manifest, field, value):
    _, data, _ = patched_manifest
    data["build"][field] = value
    with pytest.raises(SystemExit, match=field):
        release_manifest.validate_manifest(data)


def test_history_patch_is_checked_before_github_outputs(patched_manifest, tmp_path):
    path, _, patch = patched_manifest
    output = tmp_path / "outputs"
    patch.write_text("changed\n")
    with pytest.raises(SystemExit, match="patch checksum"):
        release_manifest.command_github_output(SimpleNamespace(manifest=path, github_output=output))
    assert not output.exists()


def test_release_exports_verified_history_inputs(patched_manifest, tmp_path):
    path, data, _ = patched_manifest
    output = tmp_path / "outputs"
    release_manifest.command_github_output(SimpleNamespace(manifest=path, github_output=output))
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    for key in (
        "cometbft_source_commit",
        "cometbft_patch_path",
        "cometbft_patch_sha256",
        "cometbft_history_revision",
    ):
        assert values[key] == data["build"][key]


def test_reproducible_build_passes_all_history_pins(patched_manifest, tmp_path, monkeypatch):
    import verify_release_reproducibility as repro

    _, data, _ = patched_manifest
    observed = []
    monkeypatch.setattr(repro.subprocess, "run", lambda command, **kwargs: observed.append(command))
    images = {"images": {name: {"labels": ["example=label"]} for name in ("integrated", "split")}}
    repro.build_platform_archive(
        manifest=data,
        image_release=images,
        workspace_root=tmp_path,
        target="split",
        platform="linux/arm64",
        archive_path=tmp_path / "image.tar",
    )
    assert len(observed) == 1
    for name in (
        "cometbft_source_commit",
        "cometbft_patch_path",
        "cometbft_patch_sha256",
        "cometbft_history_revision",
    ):
        assert f"{name.upper()}={data['build'][name]}" in observed[0]


@pytest.mark.parametrize("bad", [None, [], 3, "manifest"])
def test_manifest_boundary_rejects_nonobjects(bad):
    with pytest.raises(SystemExit, match="object"):
        release_manifest.validate_manifest(bad)


@pytest.mark.parametrize("section", ["components", "build", "images"])
@pytest.mark.parametrize("bad", [None, [], 3, "object"])
def test_manifest_sections_require_objects(patched_manifest, section, bad):
    _, data, _ = patched_manifest
    data[section] = bad
    with pytest.raises(SystemExit, match=section):
        release_manifest.validate_manifest(data)


@pytest.mark.parametrize("field", ["repository", "ref"])
@pytest.mark.parametrize("bad", [None, "", 3])
def test_component_coordinates_require_nonempty_strings(patched_manifest, field, bad):
    _, data, _ = patched_manifest
    data["components"]["xian-abci"][field] = bad
    with pytest.raises(SystemExit, match=field):
        release_manifest.validate_manifest(data)


def test_manifest_requires_component_record(patched_manifest):
    _, data, _ = patched_manifest
    data["components"]["xian-abci"] = None
    with pytest.raises(SystemExit, match="xian-abci"):
        release_manifest.validate_manifest(data)


@pytest.mark.parametrize("section,field", [("build", "go_image"), ("images", "split")])
@pytest.mark.parametrize("bad", [None, "", 3])
def test_required_release_values_must_be_strings(patched_manifest, section, field, bad):
    _, data, _ = patched_manifest
    data[section][field] = bad
    with pytest.raises(SystemExit, match=section + "." + field):
        release_manifest.validate_manifest(data)


def test_patch_directory_symlink_cannot_escape_release_tree(patched_manifest, tmp_path):
    path, _, patch = patched_manifest
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    patch.parent.rename(outside)
    patch.parent.symlink_to(outside, target_is_directory=True)
    with pytest.raises(SystemExit, match="patch path"):
        release_manifest.command_validate(SimpleNamespace(manifest=path))


@pytest.mark.parametrize("version", [0, 2, None, "1", True, False])
def test_release_schema_requires_integer_revision_one(patched_manifest, version):
    _, data, _ = patched_manifest
    data["schema_version"] = version
    with pytest.raises(SystemExit, match="schema_version"):
        release_manifest.validate_manifest(data)
