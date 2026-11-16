from pathlib import Path

PROJECT_ROOT = Path(__file__).absolute().parents[1]


def test_cometbft_build_uses_stable_go_build_id() -> None:
    dockerfile = (PROJECT_ROOT / "docker" / "xian-node.Dockerfile").read_text(encoding="utf-8")

    assert "go build -trimpath -buildvcs=false" in dockerfile
    assert (
        '-ldflags="-s -w -buildid= -X github.com/cometbft/cometbft/version.TMGitCommitHash='
        in dockerfile
    )
    assert ".xian-history.${COMETBFT_HISTORY_REVISION}.${COMETBFT_PATCH_SHA256}" in dockerfile


def test_cometbft_shipping_build_cannot_auto_download_another_go_toolchain():
    dockerfile = (PROJECT_ROOT / "docker/xian-node.Dockerfile").read_text()
    assert "GOTOOLCHAIN=local" in dockerfile
    assert "go version" in dockerfile
