"""Export reviewed public phase fields without keys, homes or operator bundles."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import tempfile
from pathlib import Path

from localnet_e2e_support import phase_outcome

REDACTED = "[REDACTED]"
SENSITIVE = re.compile(
    r"private|priv_key|secret|password|mnemonic|seed|founder_key|spend(?:ing)?_key|view(?:ing)?_key|(?:api|access|auth|bearer)[_-]?(?:key|token)|authorization|recovered_notes",
    re.I,
)
NEVER_FIELDS = frozenset(
    {
        "stdout",
        "stderr",
        "logs",
        "env",
        "config",
        "runtime_config",
        "operator_bundle",
        "promotion",
        "proof_config",
        "relay_proof_config",
        "wallet_checks",
        "scheduler_authorization",
    }
)
RUN_NAME = re.compile(r"[0-9]{8}T[0-9]{6}Z")
FLAGS = re.compile(
    r"(--[\w-]*(?:private[-_]key|password|secret|seed|founder[-_]key)[\w-]*)(?:=|\s+)([^\s,]+)",
    re.I,
)


def private_strings(value: object) -> set[str]:
    if isinstance(value, str):
        return {value} if value else set()
    if isinstance(value, dict):
        return set().union(*(private_strings(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(private_strings(item) for item in value))
    return set()


def collect_secrets(value: object) -> set[str]:
    secrets = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if SENSITIVE.search(key) and (isinstance(item, str) or "priv" in key.lower()):
                secrets.update(private_strings(item))
            secrets.update(collect_secrets(item))
    elif isinstance(value, list):
        for item in value:
            secrets.update(collect_secrets(item))
    return secrets


def scrub(value: str, secrets: set[str]) -> str:
    for secret in sorted(secrets, key=len, reverse=True):
        value = value.replace(secret, REDACTED)
    return FLAGS.sub(lambda match: match[1] + "=" + REDACTED, value)


def sanitize(value: object, secrets: set[str]):
    if isinstance(value, dict):
        return {
            scrub(key, secrets): REDACTED if SENSITIVE.search(key) else sanitize(item, secrets)
            for key, item in value.items()
            if key not in NEVER_FIELDS
        }
    if isinstance(value, list):
        return sanitize_arguments(value, secrets)
    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[")):
            try:
                return json.dumps(sanitize(json.loads(value), secrets))
            except (ValueError, TypeError):
                pass
        return scrub(value, secrets)
    return value


def sanitize_arguments(values: list, secrets: set[str]) -> list:
    result = []
    redact_next = False
    for value in values:
        if redact_next:
            result.append(REDACTED)
            redact_next = False
        else:
            result.append(sanitize(value, secrets))
            redact_next = (
                isinstance(value, str)
                and value.startswith("--")
                and "=" not in value
                and bool(SENSITIVE.search(value.replace("-", "_")))
            )
    return result


def public_network(value: dict) -> dict:
    result = {
        key: value[key]
        for key in ("chain_id", "genesis_network", "topology", "execution", "node_count")
        if key in value
    }
    if "nodes" in value:
        fields = {"moniker", "node_id", "account_public_key", "host_rpc_port", "host_p2p_port"}
        result["nodes"] = [
            {key: item for key, item in node.items() if key in fields} for node in value["nodes"]
        ]
    return result


def selected_fields(value: dict, fields: list[str]) -> dict:
    result = {key: value[key] for key in fields if key in value}
    if "network" in result:
        result["network"] = public_network(result["network"])
    for key in ("error", "diagnostics_error"):
        if key in value:
            result[key] = value[key]
    return result


def phase_payload(value: dict, name: str, fields: list[str], kind: str) -> dict:
    if kind == "e2e":
        if value.get("name") != name or type(value.get("ok")) is not bool:
            raise ValueError("Malformed public phase envelope")
        details = value["details"]
        result = {key: value[key] for key in ("name", "ok", "started_at", "ended_at")}
    else:
        details = value
        result = {"name": name, "ok": value.get("ok") is not False}
    result["details"] = selected_fields(details, fields)
    if kind == "e2e":
        result["status"] = phase_outcome(result["ok"], details)["status"]
    return result


def read_json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Public proof inputs must be regular files")
    return json.loads(path.read_text())


def run_directories(source: Path) -> list[Path]:
    if not source.exists():
        return []
    if source.is_symlink() or not source.is_dir():
        raise ValueError("Public proof input must be a regular directory")
    return sorted(
        path
        for path in source.iterdir()
        if RUN_NAME.fullmatch(path.name) and path.is_dir() and not path.is_symlink()
    )


def export_run(
    run: Path, destination: Path, kind: str, fields: dict, secrets: set[str]
) -> list[dict]:
    payloads = []
    for name, allowed in fields.items():
        path = run / (name + ".json")
        if path.exists() or path.is_symlink():
            value = read_json(path)
            secrets.update(collect_secrets(value))
            payloads.append((name, phase_payload(value, name, allowed, kind)))
    summaries = []
    for name, value in payloads:
        output = destination / run.name / (name + ".json")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(sanitize(value, secrets), indent=2) + "\n")
        summaries.append({key: value[key] for key in ("name", "ok", "status") if key in value})
    if summaries:
        (destination / run.name / "summary.json").write_text(
            json.dumps({"run_id": run.name, "kind": kind, "stages": summaries}, indent=2) + "\n"
        )
    return summaries


def file_manifest(destination: Path, secrets: set[str]) -> list[dict]:
    files = []
    for path in sorted(destination.rglob("*.json")):
        data = path.read_bytes()
        if any(secret.encode() in data for secret in secrets):
            raise ValueError("Unredacted signing material in public proof")
        files.append(
            {
                "path": str(path.relative_to(destination)),
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    return files


def export_proof(source: Path, destination: Path, *, kind: str, network: Path | None) -> None:
    if destination.exists():
        raise ValueError("Public proof destination already exists")
    fields = read_json(Path(__file__).with_name("localnet_public_fields.json"))[kind]
    secrets = (
        collect_secrets(read_json(network)) if network is not None and network.exists() else set()
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=destination.name + ".", dir=destination.parent))
    try:
        stages = export_runs(source, staging, kind, fields, secrets)
        manifest = {
            "schema_version": 1,
            "kind": kind,
            "stages": stages,
            "files": file_manifest(staging, secrets),
            "scope": "Allowlisted public phase fields only; no keys, bundles, configs or raw logs.",
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        staging.rename(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def export_runs(
    source: Path, destination: Path, kind: str, fields: dict, secrets: set[str]
) -> list[dict]:
    stages = []
    for run in run_directories(source):
        runtime = run / "network.json"
        if runtime.exists():
            secrets.update(collect_secrets(read_json(runtime)))
        stages.extend(export_run(run, destination, kind, fields, secrets))
    return stages


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--kind", choices=("e2e", "protocol"), required=True)
    parser.add_argument("--network", type=Path)
    args = parser.parse_args()
    export_proof(args.input, args.output, kind=args.kind, network=args.network)


if __name__ == "__main__":
    main()
