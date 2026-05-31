#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import subprocess
import tarfile
import tempfile
from pathlib import Path

SUPPORTED_PLATFORMS = ("linux/amd64", "linux/arm64")
SUPPORTED_TARGETS = ("integrated", "split")


class ReproducibilityMismatch(Exception):
    def __init__(self, message: str, *, reasons: list[str]) -> None:
        super().__init__(message)
        self.reasons = reasons


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def platform_archive_name(target: str, platform: str) -> str:
    return f"{target}-{platform.replace('/', '-')}.tar"


def expected_platform_digests(image_release: dict) -> dict[tuple[str, str], str]:
    images = image_release.get("images")
    if not isinstance(images, dict):
        raise ValueError("image release payload must contain an images object")

    expected: dict[tuple[str, str], str] = {}
    for target in SUPPORTED_TARGETS:
        image = images.get(target)
        if not isinstance(image, dict):
            raise ValueError(f"image release payload is missing images.{target}")
        platform_digests = image.get("platform_digests")
        if not isinstance(platform_digests, dict):
            raise ValueError(f"image release payload is missing images.{target}.platform_digests")
        for platform in SUPPORTED_PLATFORMS:
            digest = platform_digests.get(platform)
            if not isinstance(digest, str) or not digest.startswith("sha256:"):
                raise ValueError(
                    f"image release payload is missing a digest for {target} {platform}"
                )
            expected[(target, platform)] = digest
    return expected


def expected_platform_refs(image_release: dict) -> dict[tuple[str, str], str]:
    images = image_release.get("images")
    if not isinstance(images, dict):
        raise ValueError("image release payload must contain an images object")

    expected: dict[tuple[str, str], str] = {}
    for target in SUPPORTED_TARGETS:
        image = images.get(target)
        if not isinstance(image, dict):
            raise ValueError(f"image release payload is missing images.{target}")
        repository = image.get("repository")
        if not isinstance(repository, str) or not repository:
            raise ValueError(f"image release payload is missing images.{target}.repository")
        platform_digests = image.get("platform_digests")
        if not isinstance(platform_digests, dict):
            raise ValueError(f"image release payload is missing images.{target}.platform_digests")
        for platform in SUPPORTED_PLATFORMS:
            digest = platform_digests.get(platform)
            if not isinstance(digest, str) or not digest.startswith("sha256:"):
                raise ValueError(
                    f"image release payload is missing a digest for {target} {platform}"
                )
            expected[(target, platform)] = f"{repository}@{digest}"
    return expected


def expected_image_labels(image_release: dict) -> dict[str, list[str]]:
    images = image_release.get("images")
    if not isinstance(images, dict):
        raise ValueError("image release payload must contain an images object")

    expected: dict[str, list[str]] = {}
    for target in SUPPORTED_TARGETS:
        image = images.get(target)
        if not isinstance(image, dict):
            raise ValueError(f"image release payload is missing images.{target}")
        labels = image.get("labels")
        if not isinstance(labels, list) or not labels:
            raise ValueError(f"image release payload is missing images.{target}.labels")
        if not all(isinstance(label, str) and "=" in label for label in labels):
            raise ValueError(f"image release payload has invalid labels for images.{target}")
        expected[target] = labels
    return expected


def _read_oci_blob(archive: tarfile.TarFile, digest: str) -> dict:
    algorithm, encoded_digest = digest.split(":", 1)
    blob_name = f"blobs/{algorithm}/{encoded_digest}"
    blob_member = next(
        (
            member.name
            for member in archive.getmembers()
            if member.name == blob_name or member.name.endswith(f"/{blob_name}")
        ),
        None,
    )
    if blob_member is None:
        raise ValueError(f"OCI archive is missing blob {digest}")
    blob_file = archive.extractfile(blob_member)
    if blob_file is None:
        raise ValueError(f"OCI archive is missing blob {digest}")
    return json.loads(blob_file.read().decode("utf-8"))


def oci_image_config(archive_path: Path) -> tuple[str, dict]:
    with tarfile.open(archive_path, mode="r") as archive:
        index_name = next(
            (
                member.name
                for member in archive.getmembers()
                if member.name == "index.json" or member.name.endswith("/index.json")
            ),
            None,
        )
        if index_name is None:
            raise ValueError(f"OCI archive {archive_path} is missing index.json")
        index_member = archive.extractfile(index_name)
        if index_member is None:
            raise ValueError(f"OCI archive {archive_path} is missing index.json")
        index_payload = json.loads(index_member.read().decode("utf-8"))
    manifests = index_payload.get("manifests")
    if not isinstance(manifests, list) or len(manifests) != 1:
        raise ValueError(f"OCI archive {archive_path} must contain exactly one manifest entry")
    digest = manifests[0].get("digest")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise ValueError(f"OCI archive {archive_path} has an invalid manifest digest")
    with tarfile.open(archive_path, mode="r") as archive:
        manifest = _read_oci_blob(archive, digest)
        config = manifest.get("config")
        if not isinstance(config, dict):
            raise ValueError(f"OCI archive {archive_path} has no config descriptor")
        config_digest = config.get("digest")
        if not isinstance(config_digest, str) or not config_digest.startswith("sha256:"):
            raise ValueError(f"OCI archive {archive_path} has an invalid config digest")
        return digest, _read_oci_blob(archive, config_digest)


def oci_manifest_digest(archive_path: Path) -> str:
    return oci_image_config(archive_path)[0]


def remote_image_config(ref: str) -> dict:
    output = subprocess.run(
        [
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            "--format",
            "{{json .Image}}",
            ref,
        ],
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    payload = json.loads(output)
    if not isinstance(payload, dict):
        raise ValueError(f"image {ref} did not return an image config")
    return payload


def normalized_runtime_config(config: dict) -> dict:
    """Compare meaningful runtime config and ignore OCI/Docker default noise."""
    normalized: dict[str, object] = {}
    for key, value in sorted(config.items()):
        if value in (None, [], {}):
            continue
        normalized[key] = value
    return normalized


def normalized_image_config(config: dict) -> dict:
    runtime_config = config.get("config")
    rootfs = config.get("rootfs")
    if not isinstance(runtime_config, dict):
        raise ValueError("image config is missing config object")
    if not isinstance(rootfs, dict):
        raise ValueError("image config is missing rootfs object")
    return {
        "architecture": config.get("architecture"),
        "config": normalized_runtime_config(runtime_config),
        "created": config.get("created"),
        "os": config.get("os"),
        "rootfs": rootfs,
    }


def mismatch_reasons(local_config: dict, remote_config: dict) -> list[str]:
    reasons: list[str] = []
    for key in ("architecture", "created", "os", "config", "rootfs"):
        if local_config.get(key) != remote_config.get(key):
            reasons.append(key)
    return reasons


def build_platform_archive(
    *,
    workspace_root: Path,
    manifest: dict,
    image_release: dict,
    target: str,
    platform: str,
    archive_path: Path,
) -> None:
    build = manifest["build"]
    labels = expected_image_labels(image_release)[target]
    stack_root = workspace_root / "xian-stack"
    command = [
        "docker",
        "buildx",
        "build",
        str(stack_root),
        "--file",
        str(stack_root / "docker" / "xian-node.Dockerfile"),
        "--target",
        target,
        "--platform",
        platform,
        "--build-context",
        f"xian-abci={workspace_root / 'xian-abci'}",
        "--build-context",
        f"xian-configs={workspace_root / 'xian-configs'}",
        "--build-context",
        f"xian-contracting={workspace_root / 'xian-contracting'}",
        "--build-context",
        f"xian-py={workspace_root / 'xian-py'}",
        "--build-arg",
        f"PYTHON_IMAGE={build['python_image']}",
        "--build-arg",
        f"GO_IMAGE={build['go_image']}",
        "--build-arg",
        f"RUST_IMAGE={build['rust_image']}",
        "--build-arg",
        f"UV_IMAGE={build['uv_image']}",
        "--build-arg",
        f"DEBIAN_SNAPSHOT={build['debian_snapshot']}",
        "--build-arg",
        f"SOURCE_DATE_EPOCH={build['source_date_epoch']}",
        "--build-arg",
        f"PIP_VERSION={build['pip_version']}",
        "--build-arg",
        f"PACKAGING_VERSION={build['packaging_version']}",
        "--build-arg",
        f"WHEEL_VERSION={build['wheel_version']}",
        "--build-arg",
        f"MATURIN_VERSION={build['maturin_version']}",
        "--build-arg",
        f"COMETBFT_VERSION={build['cometbft_version']}",
        "--build-arg",
        f"COMETBFT_SOURCE_URL={build['cometbft_source_url']}",
        "--build-arg",
        f"COMETBFT_SOURCE_SHA256={build['cometbft_source_sha256']}",
        "--build-arg",
        f"S6_OVERLAY_VERSION={build['s6_overlay_version']}",
        "--build-arg",
        f"S6_OVERLAY_NOARCH_SHA256={build['s6_overlay_noarch_sha256']}",
        "--build-arg",
        f"S6_OVERLAY_X86_64_SHA256={build['s6_overlay_x86_64_sha256']}",
        "--build-arg",
        f"S6_OVERLAY_AARCH64_SHA256={build['s6_overlay_aarch64_sha256']}",
        "--output",
        f"type=oci,dest={archive_path},rewrite-timestamp=true",
    ]
    for label in labels:
        command.extend(["--label", label])
    subprocess.run(command, check=True)


def verify_reproducibility(
    *,
    manifest: dict,
    image_release: dict,
    workspace_root: Path,
) -> dict[str, dict[str, str | bool]]:
    expected = expected_platform_digests(image_release)
    remote_refs = expected_platform_refs(image_release)
    remote_configs = {
        key: normalized_image_config(remote_image_config(ref)) for key, ref in remote_refs.items()
    }
    observed: dict[str, dict[str, str | bool]] = {}
    with tempfile.TemporaryDirectory(prefix="xian-release-verify-") as tmp_dir:
        temp_root = Path(tmp_dir)
        for target in SUPPORTED_TARGETS:
            for platform in SUPPORTED_PLATFORMS:
                archive_path = temp_root / platform_archive_name(target, platform)
                build_platform_archive(
                    workspace_root=workspace_root,
                    manifest=manifest,
                    image_release=image_release,
                    target=target,
                    platform=platform,
                    archive_path=archive_path,
                )
                digest, local_config = oci_image_config(archive_path)
                expected_digest = expected[(target, platform)]
                key = f"{target}:{platform}"
                observed[key] = {
                    "expected_manifest_digest": expected_digest,
                    "rebuilt_manifest_digest": digest,
                    "manifest_digest_match": digest == expected_digest,
                }
                normalized_local_config = normalized_image_config(local_config)
                normalized_remote_config = remote_configs[(target, platform)]
                if normalized_local_config != normalized_remote_config:
                    reason_fields = mismatch_reasons(
                        normalized_local_config,
                        normalized_remote_config,
                    )
                    reasons = ", ".join(reason_fields)
                    raise ReproducibilityMismatch(
                        "reproducibility verification failed for "
                        f"{key}; expected manifest digest {expected_digest}, "
                        f"rebuilt manifest digest {digest}; rebuilt image "
                        "content/config does not match the published image "
                        f"(mismatched fields: {reasons})",
                        reasons=reason_fields,
                    )
    return observed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rebuild release images and compare normalized image content."
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="Path to xian-stack release-manifest.json.",
    )
    parser.add_argument(
        "--image-release",
        type=Path,
        required=True,
        help="Path to the staged image-release.json payload.",
    )
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="Workspace root containing xian-stack and sibling repos.",
    )
    parser.add_argument(
        "--soft-fail",
        action="store_true",
        help=(
            "Report content mismatches without failing. This keeps the release "
            "check advisory while the Docker build is not fully hermetic."
        ),
    )
    parser.add_argument(
        "--allow-rootfs-drift",
        action="store_true",
        help=(
            "Report rootfs-only mismatches without failing while keeping image "
            "metadata/config mismatches as hard failures."
        ),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    manifest = load_json(args.manifest)
    image_release = load_json(args.image_release)
    try:
        observed = verify_reproducibility(
            manifest=manifest,
            image_release=image_release,
            workspace_root=args.workspace_root.resolve(),
        )
    except ReproducibilityMismatch as exc:
        advisory = args.soft_fail or (args.allow_rootfs_drift and exc.reasons == ["rootfs"])
        print(
            json.dumps(
                {
                    "advisory": advisory,
                    "error": str(exc),
                    "mismatched_fields": exc.reasons,
                    "ok": False,
                },
                indent=2,
                sort_keys=True,
            )
        )
        if advisory:
            return 0
        return 1
    else:
        print(json.dumps({"ok": True, "observed": observed}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
