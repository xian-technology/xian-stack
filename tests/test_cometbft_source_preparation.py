from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).absolute().parents[1]
SCRIPT = ROOT / "docker/prepare-cometbft-source.sh"
COMMIT = "0880b4d378f347ab16e54ec677ff50d803f37d62"


@pytest.fixture
def inputs(tmp_path):
    source = tmp_path / "upstream"
    (source / "version").mkdir(parents=True)
    (source / "version/version.go").write_text('package version\nconst TMCoreSemVer = "0.39.0"\n')
    (source / "history.txt").write_text("stock\n")
    archive = tmp_path / "upstream.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(source, arcname="cometbft-source")
    patch = tmp_path / "history.patch"
    patch.write_text(
        "diff --git a/history.txt b/history.txt\n--- a/history.txt\n+++ b/history.txt\n"
        "@@ -1 +1 @@\n-stock\n+history-aware\n"
    )
    env = dict(
        {"PATH": os.environ["PATH"]},
        COMETBFT_VERSION="0.40.0",
        COMETBFT_SOURCE_COMMIT=COMMIT,
        COMETBFT_SOURCE_URL="https://codeload.github.com/cometbft/cometbft/tar.gz/" + COMMIT,
        COMETBFT_SOURCE_SHA256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        COMETBFT_PATCH_SHA256=hashlib.sha256(patch.read_bytes()).hexdigest(),
        COMETBFT_HISTORY_REVISION="1",
    )
    return archive, patch, tmp_path / "prepared", env


def prepare(inputs):
    archive, patch, destination, env = inputs
    return subprocess.run(
        ["bash", str(SCRIPT), str(archive), str(destination), str(patch)],
        env=env,
        capture_output=True,
        text=True,
    )


def test_prepared_source_has_verified_patch_version_and_provenance(inputs):
    result = prepare(inputs)
    assert result.returncode == 0, result.stderr
    _, _, destination, env = inputs
    assert (destination / "history.txt").read_text() == "history-aware\n"
    assert 'TMCoreSemVer = "0.40.0"' in (destination / "version/version.go").read_text()
    proof = json.loads((destination / "xian-cometbft-provenance.json").read_text())
    assert proof == {
        "version": "0.40.0",
        "source_url": env["COMETBFT_SOURCE_URL"],
        "source_commit": COMMIT,
        "source_sha256": env["COMETBFT_SOURCE_SHA256"],
        "patch_sha256": env["COMETBFT_PATCH_SHA256"],
        "history_revision": "1",
    }
    # Preparation must never mutate the caller's archive or patch input.
    assert hashlib.sha256(inputs[0].read_bytes()).hexdigest() == env["COMETBFT_SOURCE_SHA256"]
    assert hashlib.sha256(inputs[1].read_bytes()).hexdigest() == env["COMETBFT_PATCH_SHA256"]


@pytest.mark.parametrize("field", ["COMETBFT_SOURCE_SHA256", "COMETBFT_PATCH_SHA256"])
def test_wrong_input_checksum_refuses_before_extraction(inputs, field):
    inputs[3][field] = "0" * 64
    result = prepare(inputs)
    assert result.returncode != 0
    assert "checksum" in result.stderr.lower()
    assert not inputs[2].exists()


def test_unapplicable_patch_fails_without_publishing_tree(inputs):
    inputs[1].write_text("unapplicable patch\n")
    inputs[3]["COMETBFT_PATCH_SHA256"] = hashlib.sha256(inputs[1].read_bytes()).hexdigest()
    result = prepare(inputs)
    assert result.returncode != 0
    assert not inputs[2].exists()
    assert not list(inputs[2].parent.glob("prepared.stage.*"))


@pytest.mark.parametrize(
    "field,value",
    [
        ("COMETBFT_SOURCE_COMMIT", "short"),
        ("COMETBFT_HISTORY_REVISION", "2"),
        ("COMETBFT_VERSION", "0.40.0;command"),
        ("COMETBFT_VERSION", "0.39.3"),
        ("COMETBFT_SOURCE_URL", "https://example.test/unreviewed"),
    ],
)
def test_invalid_provenance_inputs_cannot_be_built(inputs, field, value):
    inputs[3][field] = value
    result = prepare(inputs)
    assert result.returncode != 0
    assert not inputs[2].exists()


def test_existing_prepared_directory_cannot_be_overwritten(inputs):
    inputs[2].mkdir()
    marker = inputs[2] / "operator-data"
    marker.write_text("preserve")
    result = prepare(inputs)
    assert result.returncode != 0
    assert marker.read_text() == "preserve"
