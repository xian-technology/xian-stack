from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from release_manifest import flatten_outputs, load_manifest, validate_manifest


class ReleaseManifestTests(unittest.TestCase):
    def test_repository_manifest_includes_pinned_release_inputs(self) -> None:
        manifest = load_manifest(
            Path(__file__).resolve().parents[1] / "release-manifest.json"
        )
        validate_manifest(manifest)
        build = manifest["build"]
        self.assertIn("@sha256:", build["python_image"])
        self.assertIn("@sha256:", build["go_image"])
        self.assertTrue(build["cometbft_source_url"].endswith(".tar.gz"))
        self.assertRegex(build["cometbft_source_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(build["s6_overlay_noarch_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(build["s6_overlay_x86_64_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(build["s6_overlay_aarch64_sha256"], r"^[0-9a-f]{64}$")

    def test_flatten_outputs_exports_new_build_fields(self) -> None:
        manifest = load_manifest(
            Path(__file__).resolve().parents[1] / "release-manifest.json"
        )
        outputs = flatten_outputs(manifest)
        for key in (
            "python_image",
            "go_image",
            "cometbft_source_url",
            "cometbft_source_sha256",
            "s6_overlay_noarch_sha256",
            "s6_overlay_x86_64_sha256",
            "s6_overlay_aarch64_sha256",
        ):
            self.assertIn(key, outputs)

    def test_validate_manifest_rejects_missing_go_image(self) -> None:
        manifest = load_manifest(
            Path(__file__).resolve().parents[1] / "release-manifest.json"
        )
        manifest["build"].pop("go_image")
        with self.assertRaisesRegex(SystemExit, "build.go_image"):
            validate_manifest(manifest)


if __name__ == "__main__":
    unittest.main()
