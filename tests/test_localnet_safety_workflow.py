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
    assert "if: ${{ !inputs.replay_run_id }}" in release
    replay = job(text, "replay-architecture")
    assert "needs: release-safety-localnet" in replay
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
