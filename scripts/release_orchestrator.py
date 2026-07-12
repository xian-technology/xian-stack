#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from release_manifest import validate_manifest

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class RepoConfig:
    name: str
    path: Path


@dataclass(frozen=True)
class ReleaseUnit:
    key: str
    repo: str
    display_name: str
    tag_glob: str
    tag_prefix: str
    include_prefixes: tuple[str, ...] = ()
    exclude_prefixes: tuple[str, ...] = ()
    trigger_units: tuple[str, ...] = ()
    stack_component: str | None = None


@dataclass
class RepoState:
    branch: str
    clean: bool
    head_sha: str
    origin_sha: str
    ahead: int
    behind: int


@dataclass(frozen=True)
class GithubCheckRun:
    name: str
    status: str
    conclusion: str | None
    url: str
    started_at: str
    completed_at: str
    database_id: int


@dataclass
class ReleasePlan:
    unit: ReleaseUnit
    latest_tag: str | None
    latest_version: str | None
    source_version: str | None
    target_version: str
    version_mode: str
    changed_files: list[str]
    reason: str
    stack_manifest_updates: dict[str, str] | None = None

    @property
    def tag(self) -> str:
        return f"{self.unit.tag_prefix}{self.target_version}"


REPOS = {
    name: RepoConfig(name=name, path=WORKSPACE_ROOT / name)
    for name in (
        "xian-contracting",
        "xian-py",
        "xian-abci",
        "xian-cli",
        "xian-linter",
        "xian-js",
        "xian-wallet-browser",
        "xian-stack",
        "xian-configs",
    )
}

XIAN_JS_PUBLISHABLE_PACKAGE_PATHS = (
    "packages/client",
    "packages/dex",
    "packages/provider",
    "packages/types",
    "packages/web-kit",
)


UNITS = {
    unit.key: unit
    for unit in (
        ReleaseUnit(
            key="xian-accounts",
            repo="xian-contracting",
            display_name="xian-tech-accounts",
            tag_glob="accounts-v*",
            tag_prefix="accounts-v",
            include_prefixes=("packages/xian-accounts/",),
            stack_component="xian-contracting",
        ),
        ReleaseUnit(
            key="xian-runtime-types",
            repo="xian-contracting",
            display_name="xian-tech-runtime-types",
            tag_glob="runtime-types-v*",
            tag_prefix="runtime-types-v",
            include_prefixes=("packages/xian-runtime-types/",),
            stack_component="xian-contracting",
        ),
        ReleaseUnit(
            key="xian-compiler-core",
            repo="xian-contracting",
            display_name="xian-tech-compiler-core",
            tag_glob="compiler-core-v*",
            tag_prefix="compiler-core-v",
            include_prefixes=("packages/xian-compiler-core/",),
            stack_component="xian-contracting",
        ),
        ReleaseUnit(
            key="xian-zk",
            repo="xian-contracting",
            display_name="xian-tech-zk",
            tag_glob="zk-v*",
            tag_prefix="zk-v",
            include_prefixes=("packages/xian-zk/",),
            stack_component="xian-contracting",
        ),
        ReleaseUnit(
            key="xian-contracting",
            repo="xian-contracting",
            display_name="xian-tech-contracting",
            tag_glob="contracting-v*",
            tag_prefix="contracting-v",
            exclude_prefixes=("packages/",),
            stack_component="xian-contracting",
        ),
        ReleaseUnit(
            key="xian-py",
            repo="xian-py",
            display_name="xian-tech-py",
            tag_glob="v*",
            tag_prefix="v",
            stack_component="xian-py",
        ),
        ReleaseUnit(
            key="xian-abci",
            repo="xian-abci",
            display_name="xian-tech-abci",
            tag_glob="v*",
            tag_prefix="v",
            stack_component="xian-abci",
        ),
        ReleaseUnit(
            key="xian-cli",
            repo="xian-cli",
            display_name="xian-tech-cli",
            tag_glob="v*",
            tag_prefix="v",
        ),
        ReleaseUnit(
            key="xian-linter",
            repo="xian-linter",
            display_name="xian-tech-linter",
            tag_glob="v*",
            tag_prefix="v",
        ),
        ReleaseUnit(
            key="xian-js",
            repo="xian-js",
            display_name="xian-js",
            tag_glob="v*",
            tag_prefix="v",
            exclude_prefixes=(
                ".github/",
                "README.md",
                "docs/",
            ),
        ),
        ReleaseUnit(
            key="xian-wallet-browser",
            repo="xian-wallet-browser",
            display_name="xian-wallet-browser",
            tag_glob="v*",
            tag_prefix="v",
            trigger_units=("xian-js",),
        ),
        ReleaseUnit(
            key="xian-stack",
            repo="xian-stack",
            display_name="xian-stack",
            tag_glob="v*",
            tag_prefix="v",
            exclude_prefixes=(
                "README.md",
                "docs/",
                "scripts/README.md",
                "scripts/release_orchestrator.py",
            ),
        ),
    )
}


RELEASE_ORDER = (
    "xian-accounts",
    "xian-runtime-types",
    "xian-compiler-core",
    "xian-zk",
    "xian-contracting",
    "xian-py",
    "xian-abci",
    "xian-cli",
    "xian-linter",
    "xian-js",
    "xian-wallet-browser",
    "xian-stack",
)

SEMVER_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z.-]+))?$"
)
PYPROJECT_VERSION_RE = re.compile(r'^version = "([^"]+)"$', re.MULTILINE)
MODULE_VERSION_RE = re.compile(r'^__version__ = "([^"]+)"$', re.MULTILINE)
GITHUB_OWNER = "xian-technology"
GREEN_CHECK_CONCLUSIONS = {"success", "neutral", "skipped"}


class ReleaseError(RuntimeError):
    pass


def run(
    args: list[str],
    *,
    cwd: Path,
    capture: bool = True,
) -> str:
    result = subprocess.run(
        args,
        cwd=cwd,
        check=False,
        capture_output=capture,
        text=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip() if result.stderr else ""
        stdout = result.stdout.strip() if result.stdout else ""
        detail = stderr or stdout or "command failed"
        raise ReleaseError(f"{cwd.name}: {' '.join(args)} -> {detail}")
    return result.stdout.strip() if capture else ""


def run_git(repo: RepoConfig, *args: str, capture: bool = True) -> str:
    return run(["git", *args], cwd=repo.path, capture=capture)


def run_gh(*args: str) -> str:
    return run(["gh", *args], cwd=WORKSPACE_ROOT)


def fetch_repo(repo: RepoConfig) -> None:
    run_git(repo, "fetch", "--tags", "origin", capture=False)


def get_repo_state(repo: RepoConfig) -> RepoState:
    branch = run_git(repo, "branch", "--show-current")
    clean = run_git(repo, "status", "--porcelain") == ""
    head_sha = run_git(repo, "rev-parse", "HEAD")
    origin_sha = run_git(repo, "rev-parse", "origin/main")
    ahead_raw, behind_raw = run_git(
        repo, "rev-list", "--left-right", "--count", "HEAD...origin/main"
    ).split()
    return RepoState(
        branch=branch,
        clean=clean,
        head_sha=head_sha,
        origin_sha=origin_sha,
        ahead=int(ahead_raw),
        behind=int(behind_raw),
    )


def latest_tag_for_unit(unit: ReleaseUnit) -> str | None:
    repo = REPOS[unit.repo]
    try:
        return run_git(
            repo, "describe", "--tags", "--abbrev=0", "--match", unit.tag_glob, "origin/main"
        )
    except ReleaseError:
        return None


def tag_exists(repo: RepoConfig, tag: str) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}"],
        cwd=repo.path,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def path_matches_prefix(path: str, prefix: str) -> bool:
    normalized = prefix.rstrip("/")
    return path == normalized or path.startswith(f"{normalized}/")


def list_changed_files(repo: RepoConfig, latest_tag: str | None) -> list[str]:
    if latest_tag is None:
        output = run_git(repo, "ls-files")
    else:
        output = run_git(repo, "diff", "--name-only", f"{latest_tag}..origin/main")
    return [line for line in output.splitlines() if line]


def relevant_changed_files(unit: ReleaseUnit, latest_tag: str | None) -> list[str]:
    files = list_changed_files(REPOS[unit.repo], latest_tag)
    selected: list[str] = []
    for path in files:
        if unit.include_prefixes and not any(
            path_matches_prefix(path, prefix) for prefix in unit.include_prefixes
        ):
            continue
        if any(path_matches_prefix(path, prefix) for prefix in unit.exclude_prefixes):
            continue
        selected.append(path)
    return selected


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def read_pyproject_version(path: Path) -> str:
    match = PYPROJECT_VERSION_RE.search(read_text(path))
    if not match:
        raise ReleaseError(f"{path}: version not found")
    return match.group(1)


def set_pyproject_version(path: Path, version: str) -> bool:
    text = read_text(path)
    updated, count = PYPROJECT_VERSION_RE.subn(f'version = "{version}"', text, count=1)
    if count != 1:
        raise ReleaseError(f"{path}: failed to update version")
    if updated == text:
        return False
    write_text(path, updated)
    return True


def read_module_version(path: Path) -> str:
    match = MODULE_VERSION_RE.search(read_text(path))
    if not match:
        raise ReleaseError(f"{path}: __version__ not found")
    return match.group(1)


def set_module_version(path: Path, version: str) -> bool:
    text = read_text(path)
    updated, count = MODULE_VERSION_RE.subn(f'__version__ = "{version}"', text, count=1)
    if count != 1:
        raise ReleaseError(f"{path}: failed to update __version__")
    if updated == text:
        return False
    write_text(path, updated)
    return True


def read_json(path: Path) -> dict:
    return json.loads(read_text(path))


def write_json(path: Path, payload: dict) -> None:
    write_text(path, json.dumps(payload, indent=2) + "\n")


def set_json_version(path: Path, version: str) -> bool:
    payload = read_json(path)
    if payload.get("version") == version:
        return False
    payload["version"] = version
    write_json(path, payload)
    return True


def chrome_extension_version(version: str) -> str:
    parsed = parse_semver(version)
    if parsed.prerelease is None:
        build = 65535
    else:
        channel = str(parsed.prerelease[0])
        offsets = {"alpha": 10000, "beta": 20000, "rc": 30000}
        if channel not in offsets:
            raise ReleaseError(f"Unsupported Chrome extension prerelease channel: {channel}")
        number = parsed.prerelease[-1] if isinstance(parsed.prerelease[-1], int) else 1
        build = offsets[channel] + number
    if build > 65535:
        raise ReleaseError(f"Chrome extension build component is too large for {version}")
    return f"{parsed.major}.{parsed.minor}.{parsed.patch}.{build}"


def set_chrome_manifest_version(path: Path, version: str) -> bool:
    payload = read_json(path)
    chrome_version = chrome_extension_version(version)
    if payload.get("version") == chrome_version and payload.get("version_name") == version:
        return False
    payload["version"] = chrome_version
    payload["version_name"] = version
    write_json(path, payload)
    return True


def update_json_dependencies(path: Path, updates: dict[str, str]) -> bool:
    payload = read_json(path)
    changed = False
    for field in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        values = payload.get(field)
        if not isinstance(values, dict):
            continue
        for name, version in updates.items():
            if name in values and values[name] != version:
                values[name] = version
                changed = True
    if changed:
        write_json(path, payload)
    return changed


def update_uv_lock_version(path: Path, project_name: str, version: str) -> bool:
    pattern = re.compile(
        rf'(\[\[package\]\]\nname = "{re.escape(project_name)}"\nversion = ")([^"]+)(")',
        re.MULTILINE,
    )
    text = read_text(path)
    updated, count = pattern.subn(rf"\g<1>{version}\3", text, count=1)
    if count != 1:
        raise ReleaseError(f"{path}: failed to update {project_name} version")
    if updated == text:
        return False
    write_text(path, updated)
    return True


def update_package_lock(
    path: Path,
    *,
    root_version: str | None = None,
    package_versions: dict[str, str] | None = None,
    dependency_updates: dict[str, dict[str, str]] | None = None,
) -> bool:
    payload = read_json(path)
    changed = False

    if root_version is not None and payload.get("version") != root_version:
        payload["version"] = root_version
        changed = True

    packages = payload.get("packages")
    if not isinstance(packages, dict):
        raise ReleaseError(f"{path}: package-lock missing packages map")

    if root_version is not None:
        root_entry = packages.get("")
        if isinstance(root_entry, dict) and root_entry.get("version") != root_version:
            root_entry["version"] = root_version
            changed = True

    for package_path, version in (package_versions or {}).items():
        entry = packages.get(package_path)
        if isinstance(entry, dict) and entry.get("version") != version:
            entry["version"] = version
            changed = True

    for package_path, updates in (dependency_updates or {}).items():
        entry = packages.get(package_path)
        if not isinstance(entry, dict):
            continue
        for field in (
            "dependencies",
            "devDependencies",
            "peerDependencies",
            "optionalDependencies",
        ):
            values = entry.get(field)
            if not isinstance(values, dict):
                continue
            for name, version in updates.items():
                if name in values and values[name] != version:
                    values[name] = version
                    changed = True

    if changed:
        write_json(path, payload)
    return changed


def ensure_matching_versions(label: str, versions: dict[str, str]) -> str:
    unique = sorted(set(versions.values()))
    if len(unique) != 1:
        formatted = ", ".join(f"{path}={value}" for path, value in sorted(versions.items()))
        raise ReleaseError(f"{label}: version mismatch ({formatted})")
    return unique[0]


def read_source_version(unit: ReleaseUnit) -> str | None:
    repo_path = REPOS[unit.repo].path
    if unit.key == "xian-abci":
        return read_module_version(repo_path / "src/abci/__init__.py")
    if unit.key == "xian-js":
        return ensure_matching_versions(
            "xian-js",
            {
                "package.json": read_json(repo_path / "package.json")["version"],
                **{
                    f"{package_path}/package.json": read_json(
                        repo_path / package_path / "package.json"
                    )["version"]
                    for package_path in XIAN_JS_PUBLISHABLE_PACKAGE_PATHS
                },
            },
        )
    if unit.key == "xian-wallet-browser":
        extension_manifest = read_json(repo_path / "apps/wallet-extension/public/manifest.json")
        return ensure_matching_versions(
            "xian-wallet-browser",
            {
                "package.json": read_json(repo_path / "package.json")["version"],
                "packages/wallet-core/package.json": read_json(
                    repo_path / "packages/wallet-core/package.json"
                )["version"],
                "apps/wallet-extension/package.json": read_json(
                    repo_path / "apps/wallet-extension/package.json"
                )["version"],
                "apps/wallet-extension/public/manifest.json version_name": extension_manifest.get(
                    "version_name", extension_manifest["version"]
                ),
            },
        )
    if unit.key == "xian-stack":
        return None
    if unit.key == "xian-compiler-core":
        return ensure_matching_versions(
            "xian-compiler-core",
            {
                "packages/xian-compiler-core/pyproject.toml": read_pyproject_version(
                    repo_path / "packages/xian-compiler-core/pyproject.toml"
                ),
                "packages/xian-compiler-core/npm/package.json": read_json(
                    repo_path / "packages/xian-compiler-core/npm/package.json"
                )["version"],
            },
        )
    if unit.key in {"xian-accounts", "xian-runtime-types", "xian-zk"}:
        package_dir = {
            "xian-accounts": "packages/xian-accounts",
            "xian-runtime-types": "packages/xian-runtime-types",
            "xian-zk": "packages/xian-zk",
        }[unit.key]
        return read_pyproject_version(repo_path / package_dir / "pyproject.toml")
    return read_pyproject_version(repo_path / "pyproject.toml")


@dataclass(frozen=True)
class SemVer:
    major: int
    minor: int
    patch: int
    prerelease: tuple[str | int, ...] | None


def parse_semver(version: str) -> SemVer:
    match = SEMVER_RE.fullmatch(version)
    if not match:
        raise ReleaseError(f"Unsupported version format: {version}")
    prerelease_text = match.group("prerelease")
    prerelease: tuple[str | int, ...] | None = None
    if prerelease_text is not None:
        parts: list[str | int] = []
        for part in prerelease_text.split("."):
            if part.isdigit():
                parts.append(int(part))
            else:
                parts.append(part)
        prerelease = tuple(parts)
    return SemVer(
        major=int(match.group("major")),
        minor=int(match.group("minor")),
        patch=int(match.group("patch")),
        prerelease=prerelease,
    )


def compare_semver(left: str, right: str) -> int:
    left_version = parse_semver(left)
    right_version = parse_semver(right)
    left_core = (left_version.major, left_version.minor, left_version.patch)
    right_core = (right_version.major, right_version.minor, right_version.patch)
    if left_core != right_core:
        return 1 if left_core > right_core else -1
    if left_version.prerelease is None and right_version.prerelease is None:
        return 0
    if left_version.prerelease is None:
        return 1
    if right_version.prerelease is None:
        return -1
    for left_part, right_part in zip(left_version.prerelease, right_version.prerelease):
        if left_part == right_part:
            continue
        if isinstance(left_part, int) and isinstance(right_part, int):
            return 1 if left_part > right_part else -1
        if isinstance(left_part, int):
            return -1
        if isinstance(right_part, int):
            return 1
        return 1 if left_part > right_part else -1
    if len(left_version.prerelease) == len(right_version.prerelease):
        return 0
    return 1 if len(left_version.prerelease) > len(right_version.prerelease) else -1


def format_semver(version: SemVer) -> str:
    base = f"{version.major}.{version.minor}.{version.patch}"
    if version.prerelease is None:
        return base
    suffix = ".".join(str(part) for part in version.prerelease)
    return f"{base}-{suffix}"


def bump_version(version: str, bump: str) -> str:
    parsed = parse_semver(version)
    if bump == "major":
        return f"{parsed.major + 1}.0.0"
    if bump == "minor":
        return f"{parsed.major}.{parsed.minor + 1}.0"
    if parsed.prerelease:
        prerelease = list(parsed.prerelease)
        if isinstance(prerelease[-1], int):
            prerelease[-1] = prerelease[-1] + 1
        else:
            prerelease.append(1)
        return format_semver(
            SemVer(
                major=parsed.major,
                minor=parsed.minor,
                patch=parsed.patch,
                prerelease=tuple(prerelease),
            )
        )
    return f"{parsed.major}.{parsed.minor}.{parsed.patch + 1}"


def strip_prerelease(version: SemVer) -> SemVer:
    return SemVer(
        major=version.major,
        minor=version.minor,
        patch=version.patch,
        prerelease=None,
    )


def bump_semver_core(version: SemVer, bump: str) -> SemVer:
    if bump == "major":
        return SemVer(version.major + 1, 0, 0, None)
    if bump == "minor":
        return SemVer(version.major, version.minor + 1, 0, None)
    return SemVer(version.major, version.minor, version.patch + 1, None)


def prerelease_number(version: SemVer, channel: str) -> int:
    prerelease = version.prerelease
    if prerelease and prerelease[0] == channel and isinstance(prerelease[-1], int):
        return prerelease[-1] + 1
    return 1


def make_prerelease(base: SemVer, channel: str, number: int) -> str:
    return format_semver(
        SemVer(
            major=base.major,
            minor=base.minor,
            patch=base.patch,
            prerelease=(channel, number),
        )
    )


def version_from_tag(unit: ReleaseUnit, tag: str | None) -> str | None:
    if tag is None:
        return None
    if not tag.startswith(unit.tag_prefix):
        raise ReleaseError(f"{unit.key}: tag {tag} does not match prefix {unit.tag_prefix}")
    return tag[len(unit.tag_prefix) :]


def choose_target_version(
    unit: ReleaseUnit,
    latest_version: str | None,
    source_version: str | None,
    bump: str,
    prerelease_channel: str | None = None,
) -> tuple[str, str]:
    if prerelease_channel is not None:
        return choose_prerelease_target_version(
            unit,
            latest_version,
            source_version,
            bump,
            prerelease_channel,
        )

    if unit.key == "xian-stack":
        if latest_version is None:
            raise ReleaseError("xian-stack requires at least one existing tag")
        return bump_version(latest_version, bump), "bump"

    if source_version is None:
        raise ReleaseError(f"{unit.key}: source version is required")
    if latest_version is None:
        return source_version, "initial"

    comparison = compare_semver(source_version, latest_version)
    if comparison < 0:
        raise ReleaseError(
            f"{unit.key}: source version {source_version} is behind latest tag {latest_version}"
        )
    if comparison == 0:
        return bump_version(latest_version, bump), "bump"
    return source_version, "prebumped"


def choose_prerelease_target_version(
    unit: ReleaseUnit,
    latest_version: str | None,
    source_version: str | None,
    bump: str,
    channel: str,
) -> tuple[str, str]:
    if unit.key == "xian-stack":
        if latest_version is None:
            raise ReleaseError("xian-stack requires at least one existing tag")
        latest = parse_semver(latest_version)
        if latest.prerelease is None:
            base = bump_semver_core(latest, bump)
        else:
            base = strip_prerelease(latest)
        return make_prerelease(base, channel, prerelease_number(latest, channel)), f"{channel}-bump"

    if source_version is None:
        raise ReleaseError(f"{unit.key}: source version is required")

    source = parse_semver(source_version)
    if latest_version is None:
        return make_prerelease(strip_prerelease(source), channel, 1), f"{channel}-initial"

    comparison = compare_semver(source_version, latest_version)
    if comparison < 0:
        raise ReleaseError(
            f"{unit.key}: source version {source_version} is behind latest tag {latest_version}"
        )

    latest = parse_semver(latest_version)
    if comparison == 0:
        base = bump_semver_core(latest, bump)
        return make_prerelease(base, channel, 1), f"{channel}-bump"

    if source.prerelease and source.prerelease[0] == channel:
        return source_version, f"{channel}-prebumped"
    return make_prerelease(strip_prerelease(source), channel, 1), f"{channel}-prebumped"


def build_stack_manifest_updates(plans_by_key: dict[str, ReleasePlan]) -> dict[str, str]:
    manifest_path = REPOS["xian-stack"].path / "release-manifest.json"
    current_manifest = json.loads(read_text(manifest_path))
    validate_manifest(current_manifest)
    current_components = current_manifest["components"]

    updates: dict[str, str] = {}

    for component in ("xian-abci", "xian-configs", "xian-contracting", "xian-py"):
        repo_name = component
        target_sha = run_git(REPOS[repo_name], "rev-parse", "origin/main")

        if component == "xian-abci" and "xian-abci" in plans_by_key:
            target_sha = run_git(REPOS["xian-abci"], "rev-parse", "HEAD")
        if component == "xian-py" and "xian-py" in plans_by_key:
            target_sha = run_git(REPOS["xian-py"], "rev-parse", "HEAD")
        if component == "xian-contracting":
            contracting_units = {
                "xian-accounts",
                "xian-runtime-types",
                "xian-compiler-core",
                "xian-zk",
                "xian-contracting",
            }
            if contracting_units.intersection(plans_by_key):
                target_sha = run_git(REPOS["xian-contracting"], "rev-parse", "HEAD")

        current_ref = current_components[component]["ref"]
        if current_ref != target_sha:
            updates[component] = target_sha

    return updates


def plan_releases(
    repo_states: dict[str, RepoState],
    *,
    bump: str,
    prerelease_channel: str | None = None,
    force_all: bool = False,
    skip_units: set[str] | None = None,
) -> list[ReleasePlan]:
    plans: list[ReleasePlan] = []
    plans_by_key: dict[str, ReleasePlan] = {}
    skipped = skip_units or set()

    for key in RELEASE_ORDER:
        if key in skipped:
            continue
        unit = UNITS[key]
        latest_tag = latest_tag_for_unit(unit)
        latest_version = version_from_tag(unit, latest_tag)

        if unit.key == "xian-stack":
            direct_changes = relevant_changed_files(unit, latest_tag)
            manifest_updates = build_stack_manifest_updates(plans_by_key)
            future_component_releases = sorted(
                {
                    planned.unit.stack_component
                    for planned in plans_by_key.values()
                    if planned.unit.stack_component in {"xian-abci", "xian-contracting", "xian-py"}
                }
            )
            reason_parts: list[str] = []
            if direct_changes:
                reason_parts.append(
                    f"{len(direct_changes)} repo file(s) changed since {latest_tag}"
                )
            if manifest_updates:
                reason_parts.append(
                    "release-manifest.json will advance "
                    + ", ".join(
                        f"{name}@{sha[:12]}" for name, sha in sorted(manifest_updates.items())
                    )
                )
            if future_component_releases:
                reason_parts.append(
                    "upstream release commits will advance " + ", ".join(future_component_releases)
                )
            if force_all and not reason_parts:
                reason_parts.append("forced release requested")
            if not reason_parts:
                continue
            target_version, version_mode = choose_target_version(
                unit,
                latest_version,
                None,
                bump,
                prerelease_channel,
            )
            if tag_exists(REPOS[unit.repo], f"{unit.tag_prefix}{target_version}"):
                raise ReleaseError(
                    f"{unit.key}: tag {unit.tag_prefix}{target_version} already exists"
                )
            plan = ReleasePlan(
                unit=unit,
                latest_tag=latest_tag,
                latest_version=latest_version,
                source_version=None,
                target_version=target_version,
                version_mode=version_mode,
                changed_files=direct_changes,
                reason="; ".join(reason_parts),
                stack_manifest_updates=manifest_updates,
            )
            plans.append(plan)
            plans_by_key[unit.key] = plan
            continue

        changed_files = relevant_changed_files(unit, latest_tag)
        reason_parts = []
        if changed_files:
            if latest_tag is None:
                reason_parts.append(
                    f"{len(changed_files)} tracked file(s) present for initial release"
                )
            else:
                reason_parts.append(f"{len(changed_files)} file(s) changed since {latest_tag}")
        for dependency in unit.trigger_units:
            if dependency in plans_by_key:
                dependency_plan = plans_by_key[dependency]
                reason_parts.append(f"{dependency} will release {dependency_plan.target_version}")
        if force_all and not reason_parts:
            reason_parts.append("forced release requested")
        if not reason_parts:
            continue

        source_version = read_source_version(unit)
        target_version, version_mode = choose_target_version(
            unit,
            latest_version,
            source_version,
            bump,
            prerelease_channel,
        )
        if tag_exists(REPOS[unit.repo], f"{unit.tag_prefix}{target_version}"):
            raise ReleaseError(f"{unit.key}: tag {unit.tag_prefix}{target_version} already exists")

        plan = ReleasePlan(
            unit=unit,
            latest_tag=latest_tag,
            latest_version=latest_version,
            source_version=source_version,
            target_version=target_version,
            version_mode=version_mode,
            changed_files=changed_files,
            reason="; ".join(reason_parts),
        )
        plans.append(plan)
        plans_by_key[unit.key] = plan

    return plans


def record_change(changed_paths: set[Path], path: Path, changed: bool) -> None:
    if changed:
        changed_paths.add(path)


def sync_unit_files(plan: ReleasePlan, plans_by_key: dict[str, ReleasePlan]) -> list[Path]:
    repo_path = REPOS[plan.unit.repo].path
    changed_paths: set[Path] = set()
    version = plan.target_version

    if plan.unit.key == "xian-abci":
        record_change(
            changed_paths,
            repo_path / "src/abci/__init__.py",
            set_module_version(repo_path / "src/abci/__init__.py", version),
        )
    elif plan.unit.key == "xian-js":
        record_change(
            changed_paths,
            repo_path / "package.json",
            set_json_version(repo_path / "package.json", version),
        )
        for package_path in XIAN_JS_PUBLISHABLE_PACKAGE_PATHS:
            manifest_path = repo_path / package_path / "package.json"
            record_change(
                changed_paths,
                manifest_path,
                set_json_version(manifest_path, version),
            )
        record_change(
            changed_paths,
            repo_path / "examples/browser-dapp/package.json",
            update_json_dependencies(
                repo_path / "examples/browser-dapp/package.json",
                {
                    "@xian-tech/client": version,
                    "@xian-tech/provider": version,
                },
            ),
        )
        record_change(
            changed_paths,
            repo_path / "packages/client/package.json",
            update_json_dependencies(
                repo_path / "packages/client/package.json",
                {"@xian-tech/types": version},
            ),
        )
        record_change(
            changed_paths,
            repo_path / "packages/provider/package.json",
            update_json_dependencies(
                repo_path / "packages/provider/package.json",
                {"@xian-tech/types": version},
            ),
        )
        record_change(
            changed_paths,
            repo_path / "packages/web-kit/package.json",
            update_json_dependencies(
                repo_path / "packages/web-kit/package.json",
                {"@xian-tech/provider": version},
            ),
        )
        record_change(
            changed_paths,
            repo_path / "package-lock.json",
            update_package_lock(
                repo_path / "package-lock.json",
                root_version=version,
                package_versions={
                    package_path: version
                    for package_path in XIAN_JS_PUBLISHABLE_PACKAGE_PATHS
                },
                dependency_updates={
                    "examples/browser-dapp": {
                        "@xian-tech/client": version,
                        "@xian-tech/provider": version,
                    },
                    "packages/client": {"@xian-tech/types": version},
                    "packages/provider": {"@xian-tech/types": version},
                    "packages/web-kit": {"@xian-tech/provider": version},
                },
            ),
        )
    elif plan.unit.key == "xian-wallet-browser":
        xian_js_version = plans_by_key.get("xian-js", None)
        resolved_xian_js_version = (
            xian_js_version.target_version
            if xian_js_version is not None
            else read_source_version(UNITS["xian-js"])
        )
        record_change(
            changed_paths,
            repo_path / "package.json",
            set_json_version(repo_path / "package.json", version),
        )
        record_change(
            changed_paths,
            repo_path / "packages/wallet-core/package.json",
            set_json_version(repo_path / "packages/wallet-core/package.json", version),
        )
        record_change(
            changed_paths,
            repo_path / "apps/wallet-extension/package.json",
            set_json_version(repo_path / "apps/wallet-extension/package.json", version),
        )
        record_change(
            changed_paths,
            repo_path / "apps/wallet-extension/public/manifest.json",
            set_chrome_manifest_version(
                repo_path / "apps/wallet-extension/public/manifest.json", version
            ),
        )
        record_change(
            changed_paths,
            repo_path / "packages/wallet-core/package.json",
            update_json_dependencies(
                repo_path / "packages/wallet-core/package.json",
                {
                    "@xian-tech/client": resolved_xian_js_version,
                    "@xian-tech/provider": resolved_xian_js_version,
                },
            ),
        )
        record_change(
            changed_paths,
            repo_path / "apps/wallet-extension/package.json",
            update_json_dependencies(
                repo_path / "apps/wallet-extension/package.json",
                {
                    "@xian-tech/provider": resolved_xian_js_version,
                    "@xian-tech/wallet-core": version,
                },
            ),
        )
        record_change(
            changed_paths,
            repo_path / "package-lock.json",
            update_package_lock(
                repo_path / "package-lock.json",
                root_version=version,
                package_versions={
                    "../xian-js/packages/client": resolved_xian_js_version,
                    "../xian-js/packages/provider": resolved_xian_js_version,
                    "apps/wallet-extension": version,
                    "packages/wallet-core": version,
                },
                dependency_updates={
                    "apps/wallet-extension": {
                        "@xian-tech/provider": resolved_xian_js_version,
                        "@xian-tech/wallet-core": version,
                    },
                    "packages/wallet-core": {
                        "@xian-tech/client": resolved_xian_js_version,
                        "@xian-tech/provider": resolved_xian_js_version,
                    },
                },
            ),
        )
    elif plan.unit.key == "xian-stack":
        manifest_path = repo_path / "release-manifest.json"
        manifest = json.loads(read_text(manifest_path))
        validate_manifest(manifest)
        manifest_updates = build_stack_manifest_updates(plans_by_key)
        for component, ref in manifest_updates.items():
            manifest["components"][component]["ref"] = ref
        validate_manifest(manifest)
        updated = json.dumps(manifest, indent=2) + "\n"
        if updated != read_text(manifest_path):
            write_text(manifest_path, updated)
            changed_paths.add(manifest_path)
    elif plan.unit.key == "xian-compiler-core":
        record_change(
            changed_paths,
            repo_path / "packages/xian-compiler-core/pyproject.toml",
            set_pyproject_version(
                repo_path / "packages/xian-compiler-core/pyproject.toml",
                version,
            ),
        )
        record_change(
            changed_paths,
            repo_path / "packages/xian-compiler-core/npm/package.json",
            set_json_version(
                repo_path / "packages/xian-compiler-core/npm/package.json",
                version,
            ),
        )
        lock_path = repo_path / "uv.lock"
        if lock_path.exists():
            record_change(
                changed_paths,
                lock_path,
                update_uv_lock_version(lock_path, "xian-tech-compiler-core", version),
            )
    elif plan.unit.key in {"xian-accounts", "xian-runtime-types", "xian-zk"}:
        package_dir = {
            "xian-accounts": "packages/xian-accounts",
            "xian-runtime-types": "packages/xian-runtime-types",
            "xian-zk": "packages/xian-zk",
        }[plan.unit.key]
        target_path = repo_path / package_dir / "pyproject.toml"
        record_change(changed_paths, target_path, set_pyproject_version(target_path, version))
    else:
        record_change(
            changed_paths,
            repo_path / "pyproject.toml",
            set_pyproject_version(repo_path / "pyproject.toml", version),
        )

    return sorted(changed_paths)


def commit_message(plan: ReleasePlan) -> str:
    subject = {
        "xian-accounts": "accounts",
        "xian-runtime-types": "runtime-types",
        "xian-compiler-core": "compiler-core",
        "xian-zk": "zk",
        "xian-contracting": "contracting",
        "xian-py": "xian-py",
        "xian-abci": "xian-abci",
        "xian-cli": "xian-cli",
        "xian-linter": "xian-linter",
        "xian-js": "xian-js",
        "xian-wallet-browser": "xian-wallet-browser",
        "xian-stack": "xian-stack",
    }[plan.unit.key]
    return f"release({subject}): prepare {plan.tag}"


def required_repos_for_apply(plans: list[ReleasePlan]) -> set[str]:
    required_repos = {"xian-configs"}
    required_repos.update(plan.unit.repo for plan in plans)
    if any(plan.unit.key == "xian-wallet-browser" for plan in plans):
        required_repos.add("xian-js")
    return required_repos


def ensure_apply_ready(repo_states: dict[str, RepoState], plans: list[ReleasePlan]) -> None:
    for repo_name in sorted(required_repos_for_apply(plans)):
        state = repo_states[repo_name]
        problems = []
        if state.branch != "main":
            problems.append(f"branch is {state.branch}")
        if not state.clean:
            problems.append("working tree is dirty")
        if state.ahead != 0 or state.behind != 0:
            problems.append(f"ahead/behind is {state.ahead}/{state.behind}")
        if problems:
            raise ReleaseError(f"{repo_name}: " + ", ".join(problems))


def github_check_runs(repo_name: str, sha: str) -> list[GithubCheckRun]:
    endpoint = f"repos/{GITHUB_OWNER}/{repo_name}/commits/{sha}/check-runs?per_page=100"
    output = run_gh("api", endpoint)
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ReleaseError(f"{repo_name}: failed to parse GitHub check runs") from exc

    check_runs = payload.get("check_runs")
    if not isinstance(check_runs, list):
        raise ReleaseError(f"{repo_name}: GitHub check run response is missing check_runs")

    parsed: list[GithubCheckRun] = []
    for check_run in check_runs:
        if not isinstance(check_run, dict):
            continue
        database_id_raw = check_run.get("id")
        database_id = database_id_raw if isinstance(database_id_raw, int) else 0
        conclusion = check_run.get("conclusion")
        parsed.append(
            GithubCheckRun(
                name=str(check_run.get("name") or "<unnamed>"),
                status=str(check_run.get("status") or ""),
                conclusion=str(conclusion) if conclusion is not None else None,
                url=str(check_run.get("html_url") or check_run.get("details_url") or ""),
                started_at=str(check_run.get("started_at") or ""),
                completed_at=str(check_run.get("completed_at") or ""),
                database_id=database_id,
            )
        )
    return parsed


def latest_check_runs_by_name(check_runs: list[GithubCheckRun]) -> list[GithubCheckRun]:
    latest: dict[str, GithubCheckRun] = {}
    for check_run in check_runs:
        previous = latest.get(check_run.name)
        sort_key = (
            check_run.completed_at or check_run.started_at,
            check_run.database_id,
        )
        previous_sort_key = (
            (previous.completed_at or previous.started_at, previous.database_id)
            if previous is not None
            else ("", 0)
        )
        if previous is None or sort_key > previous_sort_key:
            latest[check_run.name] = check_run
    return [latest[name] for name in sorted(latest)]


def ensure_github_checks_green(
    repo_states: dict[str, RepoState],
    plans: list[ReleasePlan],
) -> None:
    failures: list[str] = []
    for repo_name in sorted(required_repos_for_apply(plans)):
        state = repo_states[repo_name]
        check_runs = latest_check_runs_by_name(github_check_runs(repo_name, state.origin_sha))
        if not check_runs:
            failures.append(f"{repo_name}@{state.origin_sha[:12]}: no GitHub check runs found")
            continue
        for check_run in check_runs:
            if (
                check_run.status != "completed"
                or check_run.conclusion not in GREEN_CHECK_CONCLUSIONS
            ):
                status = f"{check_run.status}/{check_run.conclusion or 'pending'}"
                detail = f"{repo_name}@{state.origin_sha[:12]} {check_run.name}: {status}"
                if check_run.url:
                    detail += f" ({check_run.url})"
                failures.append(detail)
    if failures:
        raise ReleaseError(
            "GitHub checks are not green for the release input refs:\n- "
            + "\n- ".join(failures)
        )


def print_repo_warnings(repo_states: dict[str, RepoState]) -> None:
    warnings = []
    for repo_name in sorted(repo_states):
        state = repo_states[repo_name]
        problems = []
        if state.branch != "main":
            problems.append(f"branch={state.branch}")
        if not state.clean:
            problems.append("dirty")
        if state.ahead or state.behind:
            problems.append(f"ahead/behind={state.ahead}/{state.behind}")
        if problems:
            warnings.append(f"- {repo_name}: {', '.join(problems)}")
    if warnings:
        print("Repo warnings:")
        for line in warnings:
            print(line)
        print()


def print_plan(plans: list[ReleasePlan]) -> None:
    if not plans:
        print("No releases needed.")
        return

    print("Planned releases:")
    for index, plan in enumerate(plans, start=1):
        previous = plan.latest_version or "none"
        mode_suffix = ""
        if plan.version_mode == "prebumped":
            mode_suffix = " (uses pre-bumped source version)"
        elif plan.version_mode == "initial":
            mode_suffix = " (initial release)"
        elif plan.version_mode.endswith("-prebumped"):
            mode_suffix = " (uses pre-bumped source base)"
        elif plan.version_mode.endswith("-initial"):
            mode_suffix = " (initial prerelease)"
        elif plan.version_mode.endswith("-bump"):
            mode_suffix = " (prerelease bump)"
        version_range = f"[{previous} -> {plan.target_version}]"
        print(f"{index}. {plan.unit.key}: {plan.tag} {version_range}{mode_suffix}")
        print(f"   reason: {plan.reason}")
        if plan.changed_files:
            preview = ", ".join(plan.changed_files[:4])
            if len(plan.changed_files) > 4:
                preview += ", ..."
            print(f"   files: {preview}")


def apply_plan(plans: list[ReleasePlan], repo_states: dict[str, RepoState]) -> None:
    ensure_apply_ready(repo_states, plans)
    ensure_github_checks_green(repo_states, plans)
    plans_by_key = {plan.unit.key: plan for plan in plans}

    for plan in plans:
        repo = REPOS[plan.unit.repo]
        changed_paths = sync_unit_files(plan, plans_by_key)

        if changed_paths:
            run_git(
                repo,
                "add",
                *[str(path.relative_to(repo.path)) for path in changed_paths],
                capture=False,
            )
            run_git(repo, "commit", "-m", commit_message(plan), capture=False)
            run_git(repo, "push", "origin", "main", capture=False)

        if tag_exists(repo, plan.tag):
            raise ReleaseError(f"{repo.name}: tag {plan.tag} already exists")
        run_git(
            repo,
            "tag",
            "-a",
            plan.tag,
            "-m",
            f"{plan.unit.display_name} {plan.target_version}",
            capture=False,
        )
        run_git(repo, "push", "origin", plan.tag, capture=False)


def collect_repo_states(*, fetch: bool) -> dict[str, RepoState]:
    states: dict[str, RepoState] = {}
    for repo in REPOS.values():
        if fetch:
            fetch_repo(repo)
        states[repo.name] = get_repo_state(repo)
    return states


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Plan or apply coordinated release tags across the Xian workspace. "
            "The script releases only units whose origin/main changed since their "
            "latest matching tag."
        )
    )
    parser.add_argument(
        "--bump",
        choices=("patch", "minor", "major"),
        default="patch",
        help="Version bump to apply when a repo has not already been pre-bumped on main.",
    )
    parser.add_argument(
        "--prerelease-channel",
        choices=("alpha", "beta", "rc"),
        default=None,
        help="Create prerelease versions on this channel instead of stable releases.",
    )
    parser.add_argument(
        "--beta",
        dest="prerelease_channel",
        action="store_const",
        const="beta",
        help="Shortcut for --prerelease-channel beta.",
    )
    parser.add_argument(
        "--force-all",
        action="store_true",
        help="Plan a release for every non-skipped unit, even when no files changed.",
    )
    parser.add_argument(
        "--skip-unit",
        action="append",
        choices=tuple(sorted(UNITS)),
        default=[],
        help="Release unit to ignore for this run. May be passed more than once.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser(
        "plan", help="Print the release plan without changing any repo."
    )
    plan_parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="Inspect the current local refs without fetching origin first.",
    )

    apply_parser = subparsers.add_parser(
        "apply",
        help="Create version bumps, tags, and pushes for all planned releases.",
    )
    apply_parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="Apply using the current local refs without fetching origin first.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    fetch = not getattr(args, "no_fetch", False)
    repo_states = collect_repo_states(fetch=fetch)
    plans = plan_releases(
        repo_states,
        bump=args.bump,
        prerelease_channel=args.prerelease_channel,
        force_all=args.force_all,
        skip_units=set(args.skip_unit),
    )

    if args.command == "plan":
        print_repo_warnings(repo_states)
        print_plan(plans)
        return

    if not plans:
        print("No releases needed.")
        return

    apply_plan(plans, repo_states)
    print_plan(plans)
    print()
    print("Release tags pushed. GitHub Actions will build and publish the matching releases.")


if __name__ == "__main__":
    try:
        main()
    except ReleaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
