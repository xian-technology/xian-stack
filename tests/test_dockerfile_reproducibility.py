from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_cometbft_build_uses_stable_go_build_id() -> None:
    dockerfile = (PROJECT_ROOT / "docker" / "xian-node.Dockerfile").read_text(encoding="utf-8")

    assert 'go build -trimpath -buildvcs=false -ldflags="-s -w -buildid="' in dockerfile
