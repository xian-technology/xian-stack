#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

REQUIRED_COMPONENTS = (
    "xian-abci",
    "xian-configs",
    "xian-contracting",
    "xian-py",
)
REQUIRED_BUILD_FIELDS = (
    "python_image",
    "go_image",
    "rust_image",
    "uv_image",
    "debian_snapshot",
    "source_date_epoch",
    "pip_version",
    "packaging_version",
    "wheel_version",
    "maturin_version",
    "cometbft_version",
    "cometbft_source_url",
    "cometbft_source_sha256",
    "cometbft_source_commit",
    "cometbft_patch_path",
    "cometbft_patch_sha256",
    "cometbft_history_revision",
    "s6_overlay_version",
    "s6_overlay_noarch_sha256",
    "s6_overlay_x86_64_sha256",
    "s6_overlay_aarch64_sha256",
)
REQUIRED_IMAGE_FIELDS = ("integrated", "split")


def load_manifest(path: Path) -> dict:
    return json.loads(path.read_text())


def manifest_object(data: dict, name: str) -> dict:
    value = data.get(name)
    if not isinstance(value, dict):
        raise SystemExit(f"release manifest must contain a {name} object")
    return value


def validate_components(components: dict) -> None:
    for name in REQUIRED_COMPONENTS:
        component = components.get(name)
        if not isinstance(component, dict):
            raise SystemExit(f"release manifest missing component: {name}")
        for field in ("repository", "ref"):
            value = component.get(field)
            if not value or not isinstance(value, str):
                raise SystemExit(f"component {name} is missing a {field} value")


def validate_string_fields(values: dict, names: tuple[str, ...], prefix: str) -> None:
    for name in names:
        value = values.get(name)
        if not value or not isinstance(value, str):
            raise SystemExit(f"release manifest is missing {prefix}.{name}")


def validate_manifest(data: dict) -> None:
    if not isinstance(data, dict):
        raise SystemExit("release manifest must be an object")
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise SystemExit("release manifest schema_version must be 1")
    validate_components(manifest_object(data, "components"))
    build = manifest_object(data, "build")
    validate_string_fields(build, REQUIRED_BUILD_FIELDS, "build")
    validate_cometbft_pins(build)
    validate_string_fields(manifest_object(data, "images"), REQUIRED_IMAGE_FIELDS, "images")


def validate_cometbft_pins(build: dict) -> None:
    for name, digits in (
        ("cometbft_source_commit", 40),
        ("cometbft_source_sha256", 64),
        ("cometbft_patch_sha256", 64),
    ):
        if re.fullmatch(r"[0-9a-f]{" + str(digits) + r"}", build[name]) is None:
            raise SystemExit(f"build.{name} must be a full lowercase hexadecimal digest")
    if build["cometbft_version"] != "0.40.0":
        raise SystemExit("build.cometbft_version must be 0.40.0 for history revision 1")
    if build["cometbft_history_revision"] != "1":
        raise SystemExit("build.cometbft_history_revision must be the supported revision 1")


def checked_patch_path(root: Path, relative: str) -> Path:
    path = Path(relative)
    if (
        ".." in path.parts
        or path.parts[:2] != ("docker", "cometbft-patches")
        or path.suffix != ".patch"
    ):
        raise SystemExit("CometBFT patch path must be inside docker/cometbft-patches")
    target = root / path
    if not target.resolve().is_relative_to(root.resolve()):
        raise SystemExit("CometBFT patch path must stay inside the release source tree")
    return target


def validate_patch_file(build: dict, root: Path) -> None:
    path = checked_patch_path(root, build["cometbft_patch_path"])
    if path.is_symlink() or not path.is_file():
        raise SystemExit("CometBFT patch must be an existing regular file")
    with path.open("rb") as handle:
        actual = hashlib.file_digest(handle, "sha256").hexdigest()
    if actual != build["cometbft_patch_sha256"]:
        raise SystemExit("CometBFT patch checksum does not match the release manifest")


def validated_manifest(path: Path) -> dict:
    data = load_manifest(path)
    validate_manifest(data)
    validate_patch_file(data["build"], path.parent)
    return data


def flatten_outputs(data: dict) -> dict[str, str]:
    outputs: dict[str, str] = {}
    for name, component in data["components"].items():
        prefix = name.replace("-", "_")
        outputs[f"{prefix}_repository"] = component["repository"]
        outputs[f"{prefix}_ref"] = component["ref"]
    for name, value in data["build"].items():
        outputs[name] = value
    outputs["integrated_image_name"] = data["images"]["integrated"]
    outputs["split_image_name"] = data["images"]["split"]
    return outputs


def command_validate(args: argparse.Namespace) -> None:
    validated_manifest(args.manifest)


def command_github_output(args: argparse.Namespace) -> None:
    data = validated_manifest(args.manifest)
    outputs = flatten_outputs(data)
    with Path(args.github_output).open("a", encoding="utf-8") as handle:
        for key, value in outputs.items():
            handle.write(f"{key}={value}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate and export stack release manifest data.")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("release-manifest.json"),
        help="Path to the release manifest file.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate", help="Validate a release manifest.")
    validate_parser.set_defaults(func=command_validate)

    github_output_parser = subparsers.add_parser(
        "github-output",
        help="Write flattened manifest values to a GitHub Actions output file.",
    )
    github_output_parser.add_argument(
        "--github-output",
        required=True,
        help="Path to the GitHub Actions output file.",
    )
    github_output_parser.set_defaults(func=command_github_output)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
