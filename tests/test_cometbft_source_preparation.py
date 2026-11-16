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
    env = dict(
        {"PATH": os.environ["PATH"]},
        COMETBFT_VERSION="0.40.0",
        COMETBFT_SOURCE_COMMIT=COMMIT,
        COMETBFT_SOURCE_URL="https://codeload.github.com/cometbft/cometbft/tar.gz/" + COMMIT,
        COMETBFT_SOURCE_SHA256=hashlib.sha256(archive.read_bytes()).hexdigest(),
    )
    return archive, tmp_path / "prepared", env


def prepare(inputs):
    archive, destination, env = inputs
    return subprocess.run(
        ["bash", str(SCRIPT), str(archive), str(destination)],
        env=env,
        capture_output=True,
        text=True,
    )


def test_prepared_source_preserves_every_upstream_byte_and_records_release_provenance(inputs):
    result = prepare(inputs)
    assert result.returncode == 0, result.stderr
    archive, destination, env = inputs
    assert (destination / "history.txt").read_text() == "stock\n"
    assert 'TMCoreSemVer = "0.39.0"' in (destination / "version/version.go").read_text()
    with tarfile.open(archive) as stream:
        for member in stream.getmembers():
            if member.isfile():
                relative = Path(member.name).relative_to("cometbft-source")
                assert (destination / relative).read_bytes() == stream.extractfile(member).read()
    assert json.loads((destination / "xian-cometbft-provenance.json").read_text()) == {
        "version": "0.40.0",
        "source_url": env["COMETBFT_SOURCE_URL"],
        "source_commit": COMMIT,
        "source_sha256": env["COMETBFT_SOURCE_SHA256"],
    }
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == env["COMETBFT_SOURCE_SHA256"]


def test_wrong_archive_checksum_refuses_before_extraction(inputs):
    inputs[2]["COMETBFT_SOURCE_SHA256"] = "0" * 64
    result = prepare(inputs)
    assert result.returncode != 0
    assert "checksum" in result.stderr.lower()
    assert not inputs[1].exists()


def test_invalid_archive_leaves_no_published_or_staged_tree(inputs):
    inputs[0].write_bytes(b"not a tar archive")
    inputs[2]["COMETBFT_SOURCE_SHA256"] = hashlib.sha256(inputs[0].read_bytes()).hexdigest()
    result = prepare(inputs)
    assert result.returncode != 0
    assert not inputs[1].exists()
    assert not list(inputs[1].parent.glob("prepared.stage.*"))


@pytest.mark.parametrize(
    "field,value",
    [
        ("COMETBFT_SOURCE_COMMIT", "short"),
        ("COMETBFT_VERSION", "0.40.0;command"),
        ("COMETBFT_VERSION", "0.39.3"),
        ("COMETBFT_SOURCE_URL", "https://example.test/unreviewed"),
    ],
)
def test_invalid_provenance_inputs_cannot_be_built(inputs, field, value):
    inputs[2][field] = value
    result = prepare(inputs)
    assert result.returncode != 0
    assert not inputs[1].exists()


def test_existing_prepared_directory_cannot_be_overwritten(inputs):
    inputs[1].mkdir()
    marker = inputs[1] / "operator-data"
    marker.write_text("preserve")
    result = prepare(inputs)
    assert result.returncode != 0
    assert marker.read_text() == "preserve"
