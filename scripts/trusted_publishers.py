#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class TrustedPublisher:
    project: str
    owner: str
    repository: str
    workflow: str
    environment: str


PUBLISHERS = (
    TrustedPublisher(
        project="xian-accounts",
        owner="xian-technology",
        repository="xian-contracting",
        workflow="release.yml",
        environment="pypi-xian-accounts",
    ),
    TrustedPublisher(
        project="xian-contracting",
        owner="xian-technology",
        repository="xian-contracting",
        workflow="release.yml",
        environment="pypi-xian-contracting",
    ),
    TrustedPublisher(
        project="xian-contract-tools",
        owner="xian-technology",
        repository="xian-contracting",
        workflow="release.yml",
        environment="pypi-xian-contract-tools",
    ),
    TrustedPublisher(
        project="xian-runtime-types",
        owner="xian-technology",
        repository="xian-contracting",
        workflow="release.yml",
        environment="pypi-xian-runtime-types",
    ),
    TrustedPublisher(
        project="xian-native-tracer",
        owner="xian-technology",
        repository="xian-contracting",
        workflow="release.yml",
        environment="pypi-xian-native-tracer",
    ),
    TrustedPublisher(
        project="xian-abci",
        owner="xian-technology",
        repository="xian-abci",
        workflow="release.yml",
        environment="pypi",
    ),
    TrustedPublisher(
        project="xian-cli",
        owner="xian-technology",
        repository="xian-cli",
        workflow="release.yml",
        environment="pypi",
    ),
    TrustedPublisher(
        project="xian-py",
        owner="xian-technology",
        repository="xian-py",
        workflow="release.yml",
        environment="pypi",
    ),
    TrustedPublisher(
        project="xian-linter",
        owner="xian-technology",
        repository="xian-linter",
        workflow="release.yml",
        environment="pypi",
    ),
)


def render_json() -> str:
    return json.dumps([asdict(publisher) for publisher in PUBLISHERS], indent=2)


def render_markdown() -> str:
    lines = [
        "| PyPI project | GitHub owner | GitHub repo | Workflow filename | Environment |",
        "| --- | --- | --- | --- | --- |",
    ]
    for publisher in PUBLISHERS:
        lines.append(
            "| "
            f"`{publisher.project}` | "
            f"`{publisher.owner}` | "
            f"`{publisher.repository}` | "
            f"`{publisher.workflow}` | "
            f"`{publisher.environment}` |"
        )
    return "\n".join(lines)


def render_shell() -> str:
    lines = []
    for publisher in PUBLISHERS:
        prefix = publisher.project.upper().replace("-", "_")
        lines.extend(
            [
                f"{prefix}_PROJECT={publisher.project}",
                f"{prefix}_OWNER={publisher.owner}",
                f"{prefix}_REPOSITORY={publisher.repository}",
                f"{prefix}_WORKFLOW={publisher.workflow}",
                f"{prefix}_ENVIRONMENT={publisher.environment}",
                "",
            ]
        )
    return "\n".join(lines).rstrip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Print the exact PyPI Trusted Publisher settings for the Xian packages."
    )
    parser.add_argument(
        "format",
        choices=("json", "markdown", "shell"),
        default="markdown",
        nargs="?",
        help="Output format.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    match args.format:
        case "json":
            print(render_json())
        case "markdown":
            print(render_markdown())
        case "shell":
            print(render_shell())


if __name__ == "__main__":
    main()
