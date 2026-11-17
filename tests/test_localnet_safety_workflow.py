import re
from pathlib import Path

ROOT = Path(__file__).absolute().parents[1]


def job(text, name):
    body = text.split("\n  " + name + ":\n", 1)[1]
    return re.split(r"\n  [a-z][a-z0-9-]*:\n", body, maxsplit=1)[0]


def uploaded_paths(text):
    paths = []
    for block in re.split(r"\n      - ", text):
        if "uses: actions/upload-artifact@" not in block:
            continue
        lines = block.splitlines()
        for index, line in enumerate(lines):
            if not line.startswith("          path:"):
                continue
            value = line.split(":", 1)[1].strip()
            if value != "|":
                paths.append(value)
                continue
            for candidate in lines[index + 1 :]:
                if not candidate.startswith("            "):
                    break
                paths.append(candidate.strip())
    return paths


def test_five_node_jobs_are_serial_and_replay_only_remains_independent():
    text = (ROOT / ".github/workflows/localnet-safety.yml").read_text()
    release = job(text, "release-safety-localnet")
    assert re.search(r"^    needs: protocol-safety-localnet$", release, re.MULTILINE)
    assert "!inputs.replay_run_id && !inputs.replay_corpus_ref" in release
    replay = job(text, "replay-architecture")
    assert "needs: [release-safety-localnet, import-replay-corpus]" in replay
    assert "always()" in replay and "inputs.replay_run_id != ''" in replay
    assert "!cancelled()" in replay


def test_network_workflows_never_upload_raw_runtime_or_stage_directories():
    for name in ["localnet-safety.yml", "release.yml"]:
        text = (ROOT / ".github/workflows" / name).read_text()
        paths = uploaded_paths(text)
        assert paths
        for path in paths:
            assert ".localnet/" not in path
            assert path not in (
                "xian-stack/.artifacts/localnet-e2e",
                "xian-stack/.artifacts/localnet-protocol-safety",
            )
        assert "export_localnet_public_proof.py" in text
        assert any("public-localnet" in path for path in paths)


def test_manual_and_scheduled_safety_runs_share_one_network_slot():
    text = (ROOT / ".github/workflows/localnet-safety.yml").read_text()
    assert re.search(
        r"^concurrency:\n  group: xian-stack-network-safety\n  cancel-in-progress: false\n",
        text,
        re.MULTILINE,
    )


def test_replay_ref_inputs_are_immutable_and_both_digests_are_exposed():
    text = (ROOT / ".github/workflows/localnet-safety.yml").read_text()
    for name in ("replay_corpus_ref", "replay_archive_sha256", "replay_manifest_sha256"):
        assert re.search(r"^      " + name + r":$", text, re.MULTILINE)
    assert "immutable 40-hex" in text
    assert "archive SHA256" in text and "manifest SHA256" in text


def test_any_replay_input_prevents_both_network_jobs():
    text = (ROOT / ".github/workflows/localnet-safety.yml").read_text()
    for name in ("protocol-safety-localnet", "release-safety-localnet"):
        body = job(text, name)
        condition = re.search(r"^    if: (.+)$", body, re.MULTILINE)[1]
        for field in (
            "replay_run_id",
            "replay_corpus_ref",
            "replay_archive_sha256",
            "replay_manifest_sha256",
        ):
            assert "!inputs." + field in condition
        assert "||" not in condition


def test_ref_import_validates_before_data_checkout_and_uploads_only_validated_files():
    text = (ROOT / ".github/workflows/localnet-safety.yml").read_text()
    body = job(text, "import-replay-corpus")
    assert body.index("replay_corpus_bridge.py validate-inputs") < body.index(
        "path: incoming-corpus"
    )
    assert "ref: ${{ inputs.replay_corpus_ref }}" in body
    assert "persist-credentials: false" in body
    for field in (
        "REPLAY_RUN_ID",
        "REPLAY_CORPUS_REF",
        "REPLAY_ARCHIVE_SHA256",
        "REPLAY_MANIFEST_SHA256",
    ):
        assert field + ": ${{ inputs." in body
    assert "replay_corpus_bridge.py import" in body
    assert "--input incoming-corpus --output corpus" in body
    assert "--proof-output replay-import-proof.json" in body
    paths = uploaded_paths(body)
    assert paths == ["corpus/", "replay-import-proof.json"]
    # User-provided strings enter only through env/action arguments, never shell code.
    shell = "\n".join(line for line in body.splitlines() if line.startswith("          python"))
    assert "${{" not in shell


def test_native_replays_are_serial_and_accept_only_successful_import_or_unmixed_prior_run():
    text = (ROOT / ".github/workflows/localnet-safety.yml").read_text()
    body = job(text, "replay-architecture")
    assert "needs: [release-safety-localnet, import-replay-corpus]" in body
    assert "needs.import-replay-corpus.result == 'success'" in body
    assert "inputs.replay_run_id != '' && !inputs.replay_corpus_ref" in body
    assert "!inputs.replay_archive_sha256 && !inputs.replay_manifest_sha256" in body
    assert re.search(r"^      max-parallel: 1$", body, re.MULTILINE)


def test_encrypted_replay_decrypts_only_with_the_reviewed_helper_and_scoped_secret():
    text = (ROOT / ".github/workflows/localnet-safety.yml").read_text()
    body = job(text, "replay-architecture")
    assert "path: workflow-tools" in body and "ref: ${{ github.sha }}" in body
    assert "run: python -m pip install PyNaCl==1.6.2" in body
    assert body.count("secrets.XIAN_REPLAY_CORPUS_TEST_KEY") == 1
    decrypt = body.split("      - name: Decrypt privately", 1)[1].split("      - name:", 1)[0]
    assert "if: ${{ inputs.replay_corpus_ref }}" in decrypt
    assert "workflow-tools/scripts/replay_corpus_bridge.py decrypt" in decrypt
    assert '--input "$INPUT_CORPUS" --output "$PRIVATE_CORPUS"' in decrypt
    assert "--key-file" not in decrypt
    assert "format('{0}/xian-replay-private', runner.temp)" in body


def test_encrypted_results_never_upload_plaintext_or_raw_diagnostics_and_always_clean_up():
    text = (ROOT / ".github/workflows/localnet-safety.yml").read_text()
    body = job(text, "replay-architecture")
    public = body.split("      - name: Upload replay results", 1)[1].split("      - name:", 1)[0]
    assert "container.log" not in public
    assert "/home" not in public and "results.jsonl" not in public
    assert "architecture-applications\n" not in public
    raw = body.split("      - name: Upload ordinary replay diagnostics only", 1)[1].split(
        "      - name:", 1
    )[0]
    assert "always() && !inputs.replay_corpus_ref" in raw
    cleanup = body.split("      - name: Remove private plaintext and raw replay diagnostics", 1)[1]
    assert "always() && inputs.replay_corpus_ref" in cleanup
    assert "shutil.rmtree(os.environ['PRIVATE_CORPUS']" in cleanup
    assert "('home', 'hooks')" in cleanup and "'container.log'" in cleanup
