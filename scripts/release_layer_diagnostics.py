"""Read-only diagnostics for a published image's failed reproducibility audit.

Build with the selected tag's Dockerfile and recorded labels, compare layers,
and retain small reports/native libraries rather than an entire OCI archive.
This collects evidence; it does not replace the release verification gate.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import verify_release_reproducibility as repro


def native_evidence(data: bytes, destination: Path | None) -> list[str]:
    if destination is not None:
        destination.mkdir(parents=True, exist_ok=True)
        (destination / (hashlib.sha256(data).hexdigest() + ".so")).write_bytes(data)
    return sorted(
        {p.decode("ascii") for p in re.findall(rb"/tmp/(?:pip-|\.tmp)[A-Za-z0-9_./+-]+", data)}
    )[:30]


def wheel_inventory(data: bytes, native_dir: Path | None) -> dict:
    entries = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for member in archive.infolist():
            content = archive.read(member)
            entry = {
                "size": member.file_size,
                "mode": member.external_attr,
                "date_time": list(member.date_time),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            if member.filename.endswith(".so"):
                entry["temporary_build_paths"] = native_evidence(content, native_dir)
            entries[member.filename] = entry
    return entries


def layer_inventory(stream, native_dir: Path | None = None) -> dict:
    entries = {}
    with tarfile.open(fileobj=stream, mode="r|*") as archive:
        for member in archive:
            entry = {
                "type": member.type.decode("ascii"),
                "size": member.size,
                "mode": member.mode,
                "uid": member.uid,
                "gid": member.gid,
                "uname": member.uname,
                "gname": member.gname,
                "mtime": member.mtime,
                "linkname": member.linkname,
                "pax_headers": member.pax_headers,
            }
            if member.isfile():
                entry.update(file_inventory(archive.extractfile(member), member.name, native_dir))
            entries[member.name] = entry
    return entries


def file_inventory(stream, name: str, native_dir: Path | None) -> dict:
    if not name.endswith((".whl", ".so")):
        return {"sha256": hashlib.file_digest(stream, "sha256").hexdigest()}
    content = stream.read()
    entry = {"sha256": hashlib.sha256(content).hexdigest()}
    if name.endswith(".whl"):
        entry["wheel"] = wheel_inventory(content, native_dir)
    else:
        entry["temporary_build_paths"] = native_evidence(content, native_dir)
    return entry


def inventory_difference(expected: dict, rebuilt: dict) -> dict:
    changed = []
    names = sorted(expected.keys() & rebuilt.keys())
    for name in names:
        left, right = expected[name], rebuilt[name]
        fields = sorted(k for k in left.keys() | right.keys() if left.get(k) != right.get(k))
        if not fields:
            continue
        entry = {"path": name, "fields": fields}
        if "wheel" in fields:
            entry["wheel"] = inventory_difference(left.get("wheel", {}), right.get("wheel", {}))
        entry["expected"] = {k: left.get(k) for k in fields if k != "wheel"}
        entry["rebuilt"] = {k: right.get(k) for k in fields if k != "wheel"}
        changed.append(entry)
    return {
        "only_expected": sorted(expected.keys() - rebuilt.keys())[:100],
        "only_rebuilt": sorted(rebuilt.keys() - expected.keys())[:100],
        "changed_count": len(changed),
        "changed": changed[:100],
    }


def layer_comparison(expected: dict, rebuilt: dict) -> list[dict]:
    left = expected["rootfs"]["diff_ids"]
    right = rebuilt["rootfs"]["diff_ids"]
    history = [
        h.get("created_by", "") for h in expected.get("history", []) if not h.get("empty_layer")
    ]
    return [
        {
            "index": i,
            "expected": left[i] if i < len(left) else None,
            "rebuilt": right[i] if i < len(right) else None,
            "match": i < len(left) and i < len(right) and left[i] == right[i],
            "instruction": history[i] if i < len(history) else "",
        }
        for i in range(max(len(left), len(right)))
    ]


def registry_token(repository: str) -> str:
    if not re.fullmatch(r"ghcr\.io/[a-z0-9_-]+/[a-z0-9_.-]+", repository):
        raise ValueError("diagnostics require a public GHCR repository")
    query = urllib.parse.urlencode(
        {"service": "ghcr.io", "scope": f"repository:{repository.removeprefix('ghcr.io/')}:pull"}
    )
    with urllib.request.urlopen(f"https://ghcr.io/token?{query}", timeout=60) as response:
        return json.load(response)["token"]


def download_blob(repository: str, digest: str, destination: Path, token: str) -> None:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("invalid blob digest")
    if not re.fullmatch(r"ghcr\.io/[a-z0-9_-]+/[a-z0-9_.-]+", repository):
        raise ValueError("diagnostics require a public GHCR repository")
    url = f"https://ghcr.io/v2/{repository.removeprefix('ghcr.io/')}/blobs/{digest}"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=120) as response, destination.open("wb") as target:
        shutil.copyfileobj(response, target)
    with destination.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != digest.removeprefix("sha256:"):
        raise ValueError("downloaded registry blob failed its digest check")


def inspect_manifest(ref: str) -> dict:
    result = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", "--raw", ref],
        check=True,
        capture_output=True,
    )
    return json.loads(result.stdout)


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def compare_layer_files(outer, local_layer, remote_layer, repository, temp, native_dir):
    blob = temp / "expected-layer.blob"
    # Registry tokens are short-lived; obtain one after the potentially long build.
    download_blob(repository, remote_layer["digest"], blob, registry_token(repository))
    with blob.open("rb") as stream:
        expected = layer_inventory(stream, native_dir)
    digest = local_layer["digest"].replace(":", "/", 1)
    with outer.extractfile(f"blobs/{digest}") as stream:
        rebuilt = layer_inventory(stream, native_dir)
    blob.unlink()
    return inventory_difference(expected, rebuilt)


def diagnose(manifest, image_release, workspace, target, platform, output):
    output.mkdir(parents=True, exist_ok=True)
    ref = repro.expected_platform_refs(image_release)[target, platform]
    repository = image_release["images"][target]["repository"]
    expected = repro.remote_image_config(ref)
    remote_manifest = inspect_manifest(ref)
    write_json(output / "expected-config.json", expected)
    with tempfile.TemporaryDirectory(prefix="xian-image-diagnose-") as temporary:
        temp = Path(temporary)
        archive = temp / "rebuilt.tar"
        repro.build_platform_archive(
            workspace_root=workspace,
            manifest=manifest,
            image_release=image_release,
            target=target,
            platform=platform,
            archive_path=archive,
        )
        digest, rebuilt = repro.oci_image_config(archive)
        write_json(output / "rebuilt-config.json", rebuilt)
        rows = layer_comparison(expected, rebuilt)
        report = {"published_ref": ref, "rebuilt_manifest": digest, "layers": rows}
        write_json(output / "layers.json", report)
        with tarfile.open(archive) as outer:
            local_manifest = repro._read_oci_blob(outer, digest)
            for row in rows:
                if row["match"] or row["expected"] is None or row["rebuilt"] is None:
                    continue
                i = row["index"]
                row["files"] = compare_layer_files(
                    outer,
                    local_manifest["layers"][i],
                    remote_manifest["layers"][i],
                    repository,
                    temp,
                    output / "native",
                )
                write_json(output / "layers.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--image-release", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--target", choices=repro.SUPPORTED_TARGETS, default="integrated")
    parser.add_argument("--platform", choices=repro.SUPPORTED_PLATFORMS, default="linux/amd64")
    args = parser.parse_args()
    report = diagnose(
        repro.load_json(args.manifest),
        repro.load_json(args.image_release),
        args.workspace_root,
        args.target,
        args.platform,
        args.output_dir,
    )
    print(
        json.dumps({"differing_layers": [r["index"] for r in report["layers"] if not r["match"]]})
    )


if __name__ == "__main__":
    main()
