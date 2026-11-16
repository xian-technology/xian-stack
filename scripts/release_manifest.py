#!/usr/bin/env python3

from __future__ import annotations

import argparse
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
    ):
        if re.fullmatch(r"[0-9a-f]{" + str(digits) + r"}", build[name]) is None:
            raise SystemExit(f"build.{name} must be a full lowercase hexadecimal digest")
    if build["cometbft_version"] != "0.40.0":
        raise SystemExit("build.cometbft_version must identify pinned upstream release 0.40.0")
    expected_url = (
        "https://codeload.github.com/cometbft/cometbft/tar.gz/" + build["cometbft_source_commit"]
    )
    if build["cometbft_source_url"] != expected_url:
        raise SystemExit("build.cometbft_source_url must identify the pinned source commit")
    if any(name.startswith(("cometbft_patch", "cometbft_history")) for name in build):
        raise SystemExit("CometBFT builds must use unmodified upstream source")


def validated_manifest(path: Path) -> dict:
    data = load_manifest(path)
    validate_manifest(data)
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
