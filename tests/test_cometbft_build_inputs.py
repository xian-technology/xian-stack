from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).absolute().parents[1] / "scripts"))
import release_manifest

ROOT = Path(__file__).absolute().parents[1]


@pytest.fixture
def upstream_manifest(tmp_path):
    data = release_manifest.load_manifest(ROOT / "release-manifest.json")
    for field in ("cometbft_patch_path", "cometbft_patch_sha256", "cometbft_history_revision"):
        data["build"].pop(field, None)
    path = tmp_path / "release-manifest.json"
    path.write_text(json.dumps(data))
    return path, data


@pytest.mark.parametrize(
    "field", ["cometbft_patch_path", "cometbft_patch_sha256", "cometbft_history_revision"]
)
def test_custom_comet_build_inputs_are_rejected(upstream_manifest, field):
    _, data = upstream_manifest
    data["build"][field] = "unreviewed"
    with pytest.raises(SystemExit, match="unmodified upstream"):
        release_manifest.validate_manifest(data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("cometbft_source_commit", "short"),
        ("cometbft_source_sha256", "short"),
        ("cometbft_version", "0.39.3"),
        ("cometbft_source_url", "https://example.test/source"),
    ],
)
def test_release_validation_requires_upstream_pins(upstream_manifest, field, value):
    _, data = upstream_manifest
    data["build"][field] = value
    with pytest.raises(SystemExit, match=field):
        release_manifest.validate_manifest(data)


def test_release_exports_only_upstream_inputs(upstream_manifest, tmp_path):
    path, data = upstream_manifest
    output = tmp_path / "outputs"
    release_manifest.command_github_output(SimpleNamespace(manifest=path, github_output=output))
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    for key in ("cometbft_source_commit", "cometbft_source_url", "cometbft_source_sha256"):
        assert values[key] == data["build"][key]
    assert not any("patch" in key or "history" in key for key in values)


def test_reproducible_build_passes_upstream_pins_only(upstream_manifest, tmp_path, monkeypatch):
    import verify_release_reproducibility as repro

    _, data = upstream_manifest
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
    for name in ("cometbft_source_commit", "cometbft_source_url", "cometbft_source_sha256"):
        assert f"{name.upper()}={data['build'][name]}" in observed[0]
    assert not any("COMETBFT_PATCH" in item or "COMETBFT_HISTORY" in item for item in observed[0])


@pytest.mark.parametrize("bad", [None, [], 3, "manifest"])
def test_manifest_boundary_rejects_nonobjects(bad):
    with pytest.raises(SystemExit, match="object"):
        release_manifest.validate_manifest(bad)


@pytest.mark.parametrize("section", ["components", "build", "images"])
@pytest.mark.parametrize("bad", [None, [], 3, "object"])
def test_manifest_sections_require_objects(upstream_manifest, section, bad):
    _, data = upstream_manifest
    data[section] = bad
    with pytest.raises(SystemExit, match=section):
        release_manifest.validate_manifest(data)


@pytest.mark.parametrize("field", ["repository", "ref"])
@pytest.mark.parametrize("bad", [None, "", 3])
def test_component_coordinates_require_nonempty_strings(upstream_manifest, field, bad):
    _, data = upstream_manifest
    data["components"]["xian-abci"][field] = bad
    with pytest.raises(SystemExit, match=field):
        release_manifest.validate_manifest(data)


def test_manifest_requires_component_record(upstream_manifest):
    _, data = upstream_manifest
    data["components"]["xian-abci"] = None
    with pytest.raises(SystemExit, match="xian-abci"):
        release_manifest.validate_manifest(data)


@pytest.mark.parametrize("section,field", [("build", "go_image"), ("images", "integrated")])
@pytest.mark.parametrize("bad", [None, "", 3])
def test_required_release_values_must_be_strings(upstream_manifest, section, field, bad):
    _, data = upstream_manifest
    data[section][field] = bad
    with pytest.raises(SystemExit, match=section + "." + field):
        release_manifest.validate_manifest(data)


@pytest.mark.parametrize("version", [0, 2, None, "1", True, False])
def test_release_schema_requires_integer_revision_one(upstream_manifest, version):
    _, data = upstream_manifest
    data["schema_version"] = version
    with pytest.raises(SystemExit, match="schema_version"):
        release_manifest.validate_manifest(data)
