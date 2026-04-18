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
            raise ValueError(
                f"image release payload is missing images.{target}.platform_digests"
            )
        for platform in SUPPORTED_PLATFORMS:
            digest = platform_digests.get(platform)
            if not isinstance(digest, str) or not digest.startswith("sha256:"):
                raise ValueError(
                    f"image release payload is missing a digest for "
                    f"{target} {platform}"
                )
            expected[(target, platform)] = digest
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
            raise ValueError(
                f"image release payload has invalid labels for images.{target}"
            )
        expected[target] = labels
    return expected


def oci_manifest_digest(archive_path: Path) -> str:
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
        raise ValueError(
            f"OCI archive {archive_path} must contain exactly one manifest entry"
        )
    digest = manifests[0].get("digest")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise ValueError(f"OCI archive {archive_path} has an invalid manifest digest")
    return digest


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
        f"SOURCE_DATE_EPOCH={build['source_date_epoch']}",
        "--build-arg",
        f"PIP_VERSION={build['pip_version']}",
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
        f"type=oci,dest={archive_path}",
    ]
    for label in labels:
        command.extend(["--label", label])
    subprocess.run(command, check=True)


def verify_reproducibility(
    *,
    manifest: dict,
    image_release: dict,
    workspace_root: Path,
) -> dict[str, str]:
    expected = expected_platform_digests(image_release)
    observed: dict[str, str] = {}
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
                digest = oci_manifest_digest(archive_path)
                expected_digest = expected[(target, platform)]
                key = f"{target}:{platform}"
                observed[key] = digest
                if digest != expected_digest:
                    raise SystemExit(
                        "reproducibility verification failed for "
                        f"{key}; expected {expected_digest}, observed {digest}"
                    )
    return observed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rebuild release images and compare platform digests."
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
    return parser


def main() -> int:
    args = build_parser().parse_args()
    manifest = load_json(args.manifest)
    image_release = load_json(args.image_release)
    observed = verify_reproducibility(
        manifest=manifest,
        image_release=image_release,
        workspace_root=args.workspace_root.resolve(),
    )
    print(json.dumps({"ok": True, "observed": observed}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
