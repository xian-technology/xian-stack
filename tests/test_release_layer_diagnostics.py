from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).absolute().parents[1] / "scripts"))

import release_layer_diagnostics as diag


def layer(files, *, mtime=1):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, content in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            member.mtime = mtime
            archive.addfile(member, io.BytesIO(content))
    stream.seek(0)
    return stream


def wheel(content):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(zipfile.ZipInfo("pkg/_native.so", (2000, 1, 1, 0, 0, 0)), content)
        archive.writestr(
            zipfile.ZipInfo("pkg-1.dist-info/WHEEL", (2000, 1, 1, 0, 0, 0)),
            "Generator: maturin\n",
        )
    return stream.getvalue()


def test_layer_report_separates_content_from_metadata_and_lists_members():
    expected = diag.layer_inventory(layer({"same": b"ok", "changed": b"old", "gone": b"x"}))
    rebuilt = diag.layer_inventory(layer({"same": b"ok", "changed": b"new", "added": b"x"}))
    report = diag.inventory_difference(expected, rebuilt)
    assert report["only_expected"] == ["gone"]
    assert report["only_rebuilt"] == ["added"]
    assert report["changed_count"] == 1
    assert report["changed"][0]["path"] == "changed"
    assert report["changed"][0]["fields"] == ["sha256"]
    assert report["changed"][0]["expected"] == {"sha256": hashlib.sha256(b"old").hexdigest()}
    assert report["changed"][0]["rebuilt"] == {"sha256": hashlib.sha256(b"new").hexdigest()}


def test_layer_report_does_not_hide_timestamp_drift():
    expected = diag.layer_inventory(layer({"file": b"same"}, mtime=1))
    rebuilt = diag.layer_inventory(layer({"file": b"same"}, mtime=2))
    report = diag.inventory_difference(expected, rebuilt)
    assert report["changed"][0]["fields"] == ["mtime"]


def test_changed_wheel_identifies_the_embedded_native_library():
    expected = diag.layer_inventory(layer({"pkg.whl": wheel(b"old-native")}))
    rebuilt = diag.layer_inventory(layer({"pkg.whl": wheel(b"new-native")}))
    report = diag.inventory_difference(expected, rebuilt)
    changed = report["changed"][0]
    assert changed["path"] == "pkg.whl"
    assert changed["wheel"]["changed_count"] == 1
    assert changed["wheel"]["changed"][0]["path"] == "pkg/_native.so"


def test_history_mapping_ignores_empty_layers_and_accounts_for_extra_layers():
    expected = {
        "rootfs": {"diff_ids": ["sha256:base", "sha256:old"]},
        "history": [{"created_by": "base"}, {"empty_layer": True}, {"created_by": "COPY wheels"}],
    }
    rebuilt = {"rootfs": {"diff_ids": ["sha256:base", "sha256:new", "sha256:extra"]}}
    rows = diag.layer_comparison(expected, rebuilt)
    assert rows[0]["match"] is True
    assert rows[1]["match"] is False
    assert rows[1]["instruction"] == "COPY wheels"
    assert rows[2]["expected"] is None
    assert rows[2]["match"] is False


def test_registry_inputs_cannot_select_an_arbitrary_host_or_blob_path(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("invalid registry input reached the network")

    monkeypatch.setattr(diag.urllib.request, "urlopen", forbidden)
    for repository in ["example.com/x/y", "ghcr.io/x/y/../../bad", "ghcr.io/x/y?bad"]:
        with pytest.raises(ValueError):
            diag.registry_token(repository)
    with pytest.raises(ValueError):
        diag.download_blob("ghcr.io/x/y", "../../bad", Path("unused"), "token")
    with pytest.raises(ValueError):
        diag.download_blob("example.com/x/y", "sha256:" + "a" * 64, Path("unused"), "token")


def write_oci(path, config, layers):
    def digest(data):
        return "sha256:" + hashlib.sha256(data).hexdigest()

    config_bytes = json.dumps(config).encode()
    manifest = json.dumps(
        {
            "config": {"digest": digest(config_bytes)},
            "layers": [{"digest": digest(data)} for data in layers],
        }
    ).encode()
    files = {"index.json": json.dumps({"manifests": [{"digest": digest(manifest)}]}).encode()}
    for blob in [config_bytes, manifest, *layers]:
        files["blobs/" + digest(blob).replace(":", "/")] = blob
    path.write_bytes(layer(files).getvalue())


@pytest.mark.parametrize("extra", [None, "expected", "rebuilt"])
def test_diagnosis_rebuilds_selected_input_and_retains_real_file_differences(
    tmp_path, monkeypatch, extra
):
    expected_layer, rebuilt_layer = layer({"file": b"old"}), layer({"file": b"new"})
    expected = {
        "rootfs": {"diff_ids": ["sha256:base", "sha256:old"]},
        "history": [{"created_by": "base"}, {"created_by": "install"}],
    }
    rebuilt = {"rootfs": {"diff_ids": ["sha256:base", "sha256:new"]}}
    if extra:
        (expected if extra == "expected" else rebuilt)["rootfs"]["diff_ids"].append("sha256:extra")
    release = {
        "images": {
            target: {
                "repository": "ghcr.io/x/y",
                "platform_digests": {
                    platform: "sha256:" + "a" * 64 for platform in diag.repro.SUPPORTED_PLATFORMS
                },
            }
            for target in diag.repro.SUPPORTED_TARGETS
        }
    }
    monkeypatch.setattr(diag.repro, "remote_image_config", lambda _ref: expected)
    monkeypatch.setattr(
        diag,
        "inspect_manifest",
        lambda _ref: {
            "layers": [{}, {"digest": "blob"}]
            + ([{"digest": "extra"}] if extra == "expected" else [])
        },
    )
    monkeypatch.setattr(diag, "registry_token", lambda _repository: "public-token")
    monkeypatch.setattr(
        diag, "download_blob", lambda _r, _d, p, _t: p.write_bytes(expected_layer.getvalue())
    )
    built = []

    def build(**kwargs):
        built.append(kwargs)
        write_oci(
            kwargs["archive_path"],
            rebuilt,
            [b"base", rebuilt_layer.getvalue()] + ([b"extra"] if extra == "rebuilt" else []),
        )

    monkeypatch.setattr(diag.repro, "build_platform_archive", build)
    output = tmp_path / "nested" / "report"
    report = diag.diagnose(
        {"build": "frozen"}, release, tmp_path, "integrated", "linux/amd64", output
    )
    assert built[0]["manifest"] == {"build": "frozen"}
    assert built[0]["image_release"] == release
    assert built[0]["target"] == "integrated"
    assert built[0]["platform"] == "linux/amd64"
    assert report["layers"][0]["match"] is True
    assert report["layers"][1]["files"]["changed"][0]["path"] == "file"
    assert "files" not in report["layers"][0]
    if extra:
        assert "files" not in report["layers"][2]
    assert json.loads((output / "layers.json").read_text()) == report
    assert json.loads((output / "expected-config.json").read_text()) == expected
    assert json.loads((output / "rebuilt-config.json").read_text()) == rebuilt
    # Recollecting diagnostics must safely reuse an existing evidence directory.
    assert (
        diag.diagnose({"build": "frozen"}, release, tmp_path, "integrated", "linux/amd64", output)
        == report
    )


def test_native_evidence_is_content_addressed_and_never_extracts_wheel_paths(tmp_path):
    content = b"native /tmp/pip-build-env-example/overlay/\x00"
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("../../escaped.so", content)
    destination = tmp_path / "nested" / "native"
    result = diag.wheel_inventory(data.getvalue(), destination)
    assert result["../../escaped.so"]["temporary_build_paths"] == [
        "/tmp/pip-build-env-example/overlay/"
    ]
    files = list(destination.iterdir())
    assert len(files) == 1 and files[0].name == hashlib.sha256(content).hexdigest() + ".so"
    assert files[0].read_bytes() == content
    assert not (tmp_path / "escaped.so").exists()


def test_registry_download_checks_content_digest_before_it_is_used(tmp_path, monkeypatch):
    monkeypatch.setattr(diag.urllib.request, "urlopen", lambda _request, **_kw: io.BytesIO(b"blob"))
    destination = tmp_path / "layer"
    digest = "sha256:" + hashlib.sha256(b"blob").hexdigest()
    diag.download_blob("ghcr.io/x/y", digest, destination, "public-token")
    assert destination.read_bytes() == b"blob"
    with pytest.raises(ValueError, match="digest check"):
        diag.download_blob("ghcr.io/x/y", "sha256:" + "0" * 64, destination, "public-token")


def test_public_registry_token_and_manifest_use_read_only_requests(monkeypatch):
    calls = []

    def urlopen(url, **kwargs):
        calls.append(url)
        return io.BytesIO(b'{"token":"public-token"}')

    monkeypatch.setattr(diag.urllib.request, "urlopen", urlopen)
    assert diag.registry_token("ghcr.io/x/y") == "public-token"
    assert calls[0].startswith("https://ghcr.io/token?")
    assert "repository%3Ax%2Fy%3Apull" in calls[0]
    monkeypatch.setattr(
        diag.subprocess,
        "run",
        lambda cmd, **kwargs: (
            calls.append(cmd) or type("Result", (), {"stdout": '{"layers":[]}'})()
        ),
    )
    assert diag.inspect_manifest("ghcr.io/x/y@sha256:abc") == {"layers": []}
    assert calls[-1] == [
        "docker",
        "buildx",
        "imagetools",
        "inspect",
        "--raw",
        "ghcr.io/x/y@sha256:abc",
    ]


def test_non_file_tar_members_are_reported_without_reading_targets():
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        member = tarfile.TarInfo("link")
        member.type, member.linkname = tarfile.SYMTYPE, "/not/read"
        archive.addfile(member)
    stream.seek(0)
    assert diag.layer_inventory(stream)["link"]["linkname"] == "/not/read"


def test_native_file_and_identical_entries_are_handled_without_false_differences(tmp_path):
    stream = layer({"native.so": b"elf"})
    inventory = diag.layer_inventory(stream, tmp_path)
    assert len(list(tmp_path.iterdir())) == 1
    assert diag.inventory_difference(inventory, inventory)["changed_count"] == 0


def test_cli_collects_a_report_without_claiming_the_release_gate_passed(
    tmp_path, monkeypatch, capsys
):
    manifest, release = tmp_path / "manifest.json", tmp_path / "release.json"
    manifest.write_text("{}")
    release.write_text("{}")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "diagnose",
            "--manifest",
            str(manifest),
            "--image-release",
            str(release),
            "--workspace-root",
            str(tmp_path),
            "--output-dir",
            str(tmp_path),
        ],
    )
    monkeypatch.setattr(diag, "diagnose", lambda *_args: {"layers": [{"index": 2, "match": False}]})
    diag.main()
    assert json.loads(capsys.readouterr().out) == {"differing_layers": [2]}


def test_manifest_inspection_captures_json_and_propagates_cli_failure(tmp_path, monkeypatch):
    docker = tmp_path / "docker"
    docker.write_text(
        f"#!{sys.executable}\nimport sys\n"
        "if sys.argv[-1].endswith('bad'): sys.exit(19)\n"
        "print('{\"layers\":[]}')\n"
    )
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert diag.inspect_manifest("ghcr.io/x/y@sha256:good") == {"layers": []}
    with pytest.raises(subprocess.CalledProcessError) as failure:
        diag.inspect_manifest("ghcr.io/x/y@sha256:bad")
    assert failure.value.returncode == 19


@pytest.mark.parametrize(
    "option", ["--manifest", "--image-release", "--workspace-root", "--output-dir"]
)
def test_cli_requires_each_input_before_building(tmp_path, monkeypatch, option):
    arguments = {
        name: str(tmp_path)
        for name in ["--manifest", "--image-release", "--workspace-root", "--output-dir"]
    }
    del arguments[option]
    monkeypatch.setattr(sys, "argv", ["diagnose", *[a for pair in arguments.items() for a in pair]])
    with pytest.raises(SystemExit) as failure:
        diag.main()
    assert failure.value.code == 2
