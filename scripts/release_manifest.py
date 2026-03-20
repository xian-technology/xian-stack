#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
from pathlib import Path


REQUIRED_COMPONENTS = (
    "xian-abci",
    "xian-configs",
    "xian-contracting",
    "xian-py",
)
REQUIRED_BUILD_FIELDS = (
    "python_image",
    "cometbft_version",
    "s6_overlay_version",
)
REQUIRED_IMAGE_FIELDS = ("integrated", "split")


def load_manifest(path: Path) -> dict:
    return json.loads(path.read_text())


def validate_manifest(data: dict) -> None:
    if data.get("schema_version") != 1:
        raise SystemExit("release manifest schema_version must be 1")

    components = data.get("components")
    if not isinstance(components, dict):
        raise SystemExit("release manifest must contain a components object")

    for name in REQUIRED_COMPONENTS:
        component = components.get(name)
        if not isinstance(component, dict):
            raise SystemExit(f"release manifest missing component: {name}")
        repository = component.get("repository")
        ref = component.get("ref")
        if not repository or not isinstance(repository, str):
            raise SystemExit(f"component {name} is missing a repository value")
        if not ref or not isinstance(ref, str):
            raise SystemExit(f"component {name} is missing a ref value")

    build = data.get("build")
    if not isinstance(build, dict):
        raise SystemExit("release manifest must contain a build object")
    for name in REQUIRED_BUILD_FIELDS:
        value = build.get(name)
        if not value or not isinstance(value, str):
            raise SystemExit(f"release manifest is missing build.{name}")

    images = data.get("images")
    if not isinstance(images, dict):
        raise SystemExit("release manifest must contain an images object")
    for name in REQUIRED_IMAGE_FIELDS:
        value = images.get(name)
        if not value or not isinstance(value, str):
            raise SystemExit(f"release manifest is missing images.{name}")


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
    data = load_manifest(args.manifest)
    validate_manifest(data)


def command_github_output(args: argparse.Namespace) -> None:
    data = load_manifest(args.manifest)
    validate_manifest(data)
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
