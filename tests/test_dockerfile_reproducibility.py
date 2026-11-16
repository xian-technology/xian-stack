from pathlib import Path

PROJECT_ROOT = Path(__file__).absolute().parents[1]


def test_cometbft_build_uses_stable_go_build_id() -> None:
    dockerfile = (PROJECT_ROOT / "docker" / "xian-node.Dockerfile").read_text(encoding="utf-8")

    assert "go build -trimpath -buildvcs=false" in dockerfile
    assert (
        '-ldflags="-s -w -buildid= -X github.com/cometbft/cometbft/version.TMGitCommitHash='
        in dockerfile
    )
    assert "xian-history" not in dockerfile
    assert "TMCoreSemVer=" not in dockerfile
    assert "COMETBFT_PATCH" not in dockerfile


def test_cometbft_shipping_build_cannot_auto_download_another_go_toolchain():
    dockerfile = (PROJECT_ROOT / "docker/xian-node.Dockerfile").read_text()
    assert "GOTOOLCHAIN=local" in dockerfile
    assert "go version" in dockerfile


def test_separate_native_store_inspector_is_tested_and_shipped_without_modifying_upstream():
    dockerfile = (PROJECT_ROOT / "docker/xian-node.Dockerfile").read_text()
    assert "COPY docker/node-store-inspect /tmp/node-store-inspect" in dockerfile
    assert "go test -count=1 -p 2 /tmp/node-store-inspect/main.go" in dockerfile
    assert "-o /out/xian-node-store-inspect /tmp/node-store-inspect/main.go" in dockerfile
    assert (
        "COPY --from=cometbft-builder /out/xian-node-store-inspect "
        "/usr/local/bin/xian-node-store-inspect" in dockerfile
    )


def test_runtime_image_ships_exact_standalone_host_guard_asset():
    dockerfile = (PROJECT_ROOT / "docker/xian-node.Dockerfile").read_text()
    assert (
        "COPY --chmod=0755 scripts/node_store_runtime_guard.py "
        "/usr/local/bin/xian-node-store-check-stopped" in dockerfile
    )
