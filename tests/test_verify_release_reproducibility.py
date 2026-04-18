from __future__ import annotations

import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from verify_release_reproducibility import (
    expected_platform_digests,
    expected_image_labels,
    oci_manifest_digest,
)


class VerifyReleaseReproducibilityTests(unittest.TestCase):
    def test_expected_platform_digests_requires_both_targets(self) -> None:
        payload = {
            "images": {
                "integrated": {
                    "platform_digests": {
                        "linux/amd64": "sha256:" + "1" * 64,
                        "linux/arm64": "sha256:" + "2" * 64,
                    }
                },
                "split": {
                    "platform_digests": {
                        "linux/amd64": "sha256:" + "3" * 64,
                        "linux/arm64": "sha256:" + "4" * 64,
                    }
                },
            }
        }
        resolved = expected_platform_digests(payload)
        self.assertEqual(resolved[("integrated", "linux/amd64")], "sha256:" + "1" * 64)
        self.assertEqual(resolved[("split", "linux/arm64")], "sha256:" + "4" * 64)

    def test_oci_manifest_digest_reads_single_manifest_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            archive_path = Path(tmp_dir) / "image.tar"
            index_payload = {
                "schemaVersion": 2,
                "manifests": [{"digest": "sha256:" + "a" * 64}],
            }
            with tarfile.open(archive_path, mode="w") as archive:
                encoded = json.dumps(index_payload).encode("utf-8")
                info = tarfile.TarInfo(name="index.json")
                info.size = len(encoded)
                archive.addfile(info, io.BytesIO(encoded))
            self.assertEqual(oci_manifest_digest(archive_path), "sha256:" + "a" * 64)

    def test_expected_image_labels_requires_labels_for_each_target(self) -> None:
        payload = {
            "images": {
                "integrated": {
                    "labels": [
                        "org.opencontainers.image.title=Xian Node",
                        "io.xian.release.manifest-sha=" + "a" * 64,
                    ]
                },
                "split": {
                    "labels": [
                        "org.opencontainers.image.title=Xian Node Split Runtime",
                        "io.xian.release.manifest-sha=" + "b" * 64,
                    ]
                },
            }
        }
        resolved = expected_image_labels(payload)
        self.assertEqual(
            resolved["integrated"][0], "org.opencontainers.image.title=Xian Node"
        )
        self.assertEqual(
            resolved["split"][1], "io.xian.release.manifest-sha=" + "b" * 64
        )


if __name__ == "__main__":
    unittest.main()
