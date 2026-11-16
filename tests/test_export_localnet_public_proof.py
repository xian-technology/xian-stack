from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).absolute().parents[1] / "scripts"))
import export_localnet_public_proof as exporter


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return path


def phase(name, details):
    return {
        "name": name,
        "ok": True,
        "started_at": "2026-11-16T00:00:00Z",
        "ended_at": "2026-11-16T00:00:01Z",
        "details": details,
    }


def test_export_keeps_only_reviewed_phase_fields_and_never_runtime_files(tmp_path):
    root = tmp_path / "raw"
    run = root / "20261116T000000Z"
    out = tmp_path / "public"
    secret = "ephemeral-founder-private-value"
    write(
        run / "network.json",
        {"founder_key": secret, "nodes": [{"account_private_key": "validator-private-value"}]},
    )
    write(
        run / "00-bootstrap.json",
        phase(
            "00-bootstrap",
            {
                "network": {
                    "chain_id": "test-chain",
                    "founder_key": secret,
                    "nodes": [
                        {
                            "moniker": "node-0",
                            "account_public_key": "public-address",
                            "account_private_key": secret,
                        }
                    ],
                },
                "env": {"raw": "never export"},
                "localnet_init": secret,
            },
        ),
    )
    write(
        run / "03-atomic-rollback.json",
        phase(
            "03-atomic-rollback",
            {
                "contract": "con_test",
                "failures": [
                    {"message": "known secret " + secret, "private_key": "another-private-value"}
                ],
                "unexpected_operator_dump": "not allowlisted",
            },
        ),
    )
    write(run / "operator-bundle.json", {"private_key": secret})
    write(run / "homes/config/priv_validator_key.json", {"priv_key": {"value": secret}})
    write(
        run / "15-shielded-note-token.json",
        phase(
            "15-shielded-note-token",
            {
                "token": "con_token",
                "proof_config": {"spend_key": "private-note-key"},
                "alice_recovered_notes": [secret],
                "promotion": {"operator_bundle": secret},
            },
        ),
    )
    exporter.export_proof(root, out, kind="e2e", network=None)
    files = {str(p.relative_to(out)) for p in out.rglob("*") if p.is_file()}
    assert files == {
        "20261116T000000Z/00-bootstrap.json",
        "20261116T000000Z/03-atomic-rollback.json",
        "20261116T000000Z/15-shielded-note-token.json",
        "20261116T000000Z/summary.json",
        "manifest.json",
    }
    combined = "".join(p.read_text() for p in out.rglob("*.json"))
    for value in [
        secret,
        "validator-private-value",
        "another-private-value",
        "private-note-key",
        "not allowlisted",
        "never export",
    ]:
        assert value not in combined
    first = json.loads((out / "20261116T000000Z/00-bootstrap.json").read_text())
    assert first["details"]["network"] == {
        "chain_id": "test-chain",
        "nodes": [{"moniker": "node-0", "account_public_key": "public-address"}],
    }
    assert (
        json.loads((out / "20261116T000000Z/03-atomic-rollback.json").read_text())["details"][
            "contract"
        ]
        == "con_test"
    )
    manifest = json.loads((out / "manifest.json").read_text())
    for entry in manifest["files"]:
        assert hashlib.sha256((out / entry["path"]).read_bytes()).hexdigest() == entry["sha256"]


def test_protocol_success_and_failure_are_public_without_raw_diagnostics(tmp_path):
    run = tmp_path / "raw/20261116T000000Z"
    out = tmp_path / "public"
    write(
        run / "00-bootstrap.json",
        {"network": {"chain_id": "chain", "node_count": 5}, "localnet_init": "private stdout"},
    )
    write(
        run / "07-evidence.json",
        {
            "ok": False,
            "error": "valid failure",
            "diagnostics": {"private_key": "raw-key"},
            "unexpected": "drop",
        },
    )
    exporter.export_proof(run.parent, out, kind="protocol", network=None)
    failure = json.loads((out / run.name / "07-evidence.json").read_text())
    assert failure == {"name": "07-evidence", "ok": False, "details": {"error": "valid failure"}}
    assert "private stdout" not in "".join(p.read_text() for p in out.rglob("*.json"))


def test_embedded_secrets_dictionary_keys_and_command_text_are_redacted():
    secret = "known-secret-value"
    value = {
        secret: {
            "stdout": "do not copy",
            "message": "contains " + secret,
            "private_key": "unknown-secret",
        },
        "nested_json": json.dumps({"password": "not public", "ok": "public"}),
    }
    result = exporter.sanitize(value, {secret})
    assert secret not in json.dumps(result)
    assert "unknown-secret" not in json.dumps(result)
    assert "do not copy" not in json.dumps(result)
    assert "not public" not in json.dumps(result)
    assert "public" in result["nested_json"]


def test_known_stage_symlink_is_refused_and_destination_is_never_overwritten(tmp_path):
    root = tmp_path / "raw"
    run = root / "20261116T000000Z"
    run.mkdir(parents=True)
    target = write(
        tmp_path / "outside.json", phase("03-atomic-rollback", {"contract": "con_outside"})
    )
    (run / "03-atomic-rollback.json").symlink_to(target)
    with pytest.raises(ValueError, match="regular"):
        exporter.export_proof(root, tmp_path / "public", kind="e2e", network=None)
    out = tmp_path / "existing"
    out.mkdir()
    (out / "operator-data").write_text("preserve")
    with pytest.raises(ValueError, match="exists"):
        exporter.export_proof(root, out, kind="e2e", network=None)
    assert (out / "operator-data").read_text() == "preserve"


def test_nonphase_files_and_unknown_nested_trees_are_never_read(tmp_path):
    root = tmp_path / "raw"
    root.mkdir()
    write(root / "network.json", {"private_key": "secret"})
    write(
        root / "homes/20261116T000000Z/03-atomic-rollback.json",
        phase("03-atomic-rollback", {"contract": "must-not-export"}),
    )
    exporter.export_proof(root, tmp_path / "public", kind="e2e", network=None)
    assert {p.name for p in (tmp_path / "public").iterdir()} == {"manifest.json"}


def test_late_audit_failure_never_publishes_partial_proof(tmp_path, monkeypatch):
    root = tmp_path / "raw"
    run = root / "20261116T000000Z"
    out = tmp_path / "public"
    write(run / "03-atomic-rollback.json", phase("03-atomic-rollback", {"contract": "con_test"}))

    def reject(*args):
        raise ValueError("private material found")

    monkeypatch.setattr(exporter, "file_manifest", reject)
    with pytest.raises(ValueError, match="private material"):
        exporter.export_proof(root, out, kind="e2e", network=None)
    assert not out.exists()
    assert not list(tmp_path.glob("public.*"))


def test_command_argument_lists_cannot_publish_unknown_wallet_keys():
    value = ["--private-key", "unlisted-wallet-secret", "--nodes", "5"]
    assert exporter.sanitize(value, set()) == ["--private-key", exporter.REDACTED, "--nodes", "5"]


def test_sensitive_encodings_and_free_text_flags_are_detected(tmp_path):
    secrets = exporter.collect_secrets(
        {"priv_key": {"value": "base64-signing-key"}, "seed": "wallet-seed-string", "height": 42}
    )
    assert {"base64-signing-key", "wallet-seed-string"} <= secrets
    text = exporter.sanitize("cmd --private-key unknown-key-value --nodes 5", set())
    assert "unknown-key-value" not in text and "--nodes 5" in text
    value = ["--private-key=unknown-key-value", "--nodes", "5"]
    assert exporter.sanitize(value, set()) == ["--private-key=" + exporter.REDACTED, "--nodes", "5"]
    (tmp_path / "leak.json").write_text("base64-signing-key")
    with pytest.raises(ValueError, match="signing material"):
        exporter.file_manifest(tmp_path, secrets)


def test_malformed_phase_and_source_shapes_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="envelope"):
        exporter.phase_payload(
            {"name": "wrong", "ok": True, "details": {}}, "03-atomic-rollback", [], "e2e"
        )
    source = tmp_path / "file"
    source.write_text("not a directory")
    with pytest.raises(ValueError, match="directory"):
        exporter.run_directories(source)
    assert exporter.run_directories(tmp_path / "absent") == []


def test_service_and_note_secret_fields_are_never_published():
    value = {
        "api_key": "service-credential",
        "spending_key": "note-credential",
        "view_key": "view-credential",
        "token": "con_public_token",
    }
    result = exporter.sanitize(value, set())
    assert result == {
        "api_key": exporter.REDACTED,
        "spending_key": exporter.REDACTED,
        "view_key": exporter.REDACTED,
        "token": "con_public_token",
    }


def test_short_credentials_and_overlapping_values_are_fully_redacted():
    value = {"password": "short", "message": "failed with short"}
    secrets = exporter.collect_secrets(value)
    assert "short" in secrets
    assert "short" not in json.dumps(exporter.sanitize(value, secrets))
    assert (
        exporter.scrub("prefix signing-key-long suffix", {"signing-key", "signing-key-long"})
        == "prefix " + exporter.REDACTED + " suffix"
    )
    assert exporter.private_strings("") == set()
    assert exporter.private_strings({"items": ["abc", None]}) == {"abc"}
    assert exporter.sanitize("{not-json}", set()) == "{not-json}"


def test_only_timestamped_regular_run_directories_are_selected(tmp_path):
    valid = tmp_path / "20261116T000000Z"
    valid.mkdir()
    (tmp_path / "20261116T000001Z").write_text("not a directory")
    (tmp_path / "homes").mkdir()
    (tmp_path / "20261116T000002Z").symlink_to(valid, target_is_directory=True)
    assert exporter.run_directories(tmp_path) == [valid]


def test_runtime_only_secret_is_removed_without_bootstrap_phase(tmp_path):
    root = tmp_path / "raw"
    run = root / "20261116T000000Z"
    out = tmp_path / "public"
    write(run / "network.json", {"founder_key": "runtime-only-secret"})
    write(
        run / "03-atomic-rollback.json",
        phase(
            "03-atomic-rollback",
            {"contract": "con_test", "failures": [{"message": "failed: runtime-only-secret"}]},
        ),
    )
    exporter.export_proof(root, out, kind="e2e", network=None)
    text = (out / run.name / "03-atomic-rollback.json").read_text()
    assert "runtime-only-secret" not in text and exporter.REDACTED in text
    manifest = json.loads((out / "manifest.json").read_text())
    assert type(manifest["schema_version"]) is int and manifest["schema_version"] == 1
