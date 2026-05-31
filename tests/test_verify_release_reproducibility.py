from __future__ import annotations

import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import verify_release_reproducibility as repro
from verify_release_reproducibility import (
    expected_image_labels,
    expected_platform_digests,
    expected_platform_refs,
    normalized_image_config,
    oci_manifest_digest,
)


class VerifyReleaseReproducibilityTests(unittest.TestCase):
    def _single_platform_release(self) -> dict:
        return {
            "images": {
                "integrated": {
                    "repository": "ghcr.io/xian-technology/xian-node",
                    "labels": ["org.opencontainers.image.title=Xian Node"],
                    "platform_digests": {
                        "linux/amd64": "sha256:" + "1" * 64,
                    },
                }
            }
        }

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

    def test_expected_platform_refs_combines_repository_and_platform_digest(
        self,
    ) -> None:
        payload = {
            "images": {
                "integrated": {
                    "repository": "ghcr.io/xian-technology/xian-node",
                    "platform_digests": {
                        "linux/amd64": "sha256:" + "1" * 64,
                        "linux/arm64": "sha256:" + "2" * 64,
                    },
                },
                "split": {
                    "repository": "ghcr.io/xian-technology/xian-node-split",
                    "platform_digests": {
                        "linux/amd64": "sha256:" + "3" * 64,
                        "linux/arm64": "sha256:" + "4" * 64,
                    },
                },
            }
        }
        resolved = expected_platform_refs(payload)
        self.assertEqual(
            resolved[("integrated", "linux/amd64")],
            "ghcr.io/xian-technology/xian-node@sha256:" + "1" * 64,
        )
        self.assertEqual(
            resolved[("split", "linux/arm64")],
            "ghcr.io/xian-technology/xian-node-split@sha256:" + "4" * 64,
        )

    def test_oci_manifest_digest_reads_single_manifest_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            archive_path = Path(tmp_dir) / "image.tar"
            config_digest = "sha256:" + "b" * 64
            manifest_digest = "sha256:" + "a" * 64
            index_payload = {
                "schemaVersion": 2,
                "manifests": [{"digest": manifest_digest}],
            }
            manifest_payload = {
                "schemaVersion": 2,
                "config": {"digest": config_digest},
            }
            config_payload = {
                "architecture": "amd64",
                "config": {"Env": ["PYTHONUNBUFFERED=1"]},
                "created": "2024-01-01T00:00:00Z",
                "os": "linux",
                "rootfs": {"type": "layers", "diff_ids": []},
            }
            with tarfile.open(archive_path, mode="w") as archive:
                for name, payload in (
                    ("index.json", index_payload),
                    ("blobs/sha256/" + "a" * 64, manifest_payload),
                    ("blobs/sha256/" + "b" * 64, config_payload),
                ):
                    encoded = json.dumps(payload).encode("utf-8")
                    info = tarfile.TarInfo(name=name)
                    info.size = len(encoded)
                    archive.addfile(info, io.BytesIO(encoded))
            self.assertEqual(oci_manifest_digest(archive_path), manifest_digest)

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
        self.assertEqual(resolved["integrated"][0], "org.opencontainers.image.title=Xian Node")
        self.assertEqual(resolved["split"][1], "io.xian.release.manifest-sha=" + "b" * 64)

    def test_normalized_image_config_ignores_history(self) -> None:
        payload = {
            "architecture": "amd64",
            "config": {"Env": ["PYTHONUNBUFFERED=1"], "Labels": {"name": "xian"}},
            "created": "2024-01-01T00:00:00Z",
            "history": [{"created_by": "first build"}],
            "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": ["sha256:" + "c" * 64]},
        }
        self.assertEqual(
            normalized_image_config(payload),
            {
                "architecture": "amd64",
                "config": {"Env": ["PYTHONUNBUFFERED=1"], "Labels": {"name": "xian"}},
                "created": "2024-01-01T00:00:00Z",
                "os": "linux",
                "rootfs": {"type": "layers", "diff_ids": ["sha256:" + "c" * 64]},
            },
        )

    def test_reproducibility_allows_manifest_digest_drift_when_content_matches(self) -> None:
        image_config = {
            "architecture": "amd64",
            "config": {"Env": ["PYTHONUNBUFFERED=1"]},
            "created": "2024-01-01T00:00:00Z",
            "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": ["sha256:" + "c" * 64]},
        }
        with (
            mock.patch.object(repro, "SUPPORTED_TARGETS", ("integrated",)),
            mock.patch.object(repro, "SUPPORTED_PLATFORMS", ("linux/amd64",)),
            mock.patch.object(repro, "remote_image_config", return_value=image_config),
            mock.patch.object(repro, "build_platform_archive"),
            mock.patch.object(
                repro,
                "oci_image_config",
                return_value=("sha256:" + "2" * 64, image_config),
            ),
        ):
            observed = repro.verify_reproducibility(
                manifest={"build": {}},
                image_release=self._single_platform_release(),
                workspace_root=Path("/workspace"),
            )

        self.assertFalse(observed["integrated:linux/amd64"]["manifest_digest_match"])

    def test_reproducibility_still_fails_when_content_differs(self) -> None:
        remote_config = {
            "architecture": "amd64",
            "config": {"Env": ["PYTHONUNBUFFERED=1"]},
            "created": "2024-01-01T00:00:00Z",
            "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": ["sha256:" + "c" * 64]},
        }
        local_config = {
            **remote_config,
            "rootfs": {"type": "layers", "diff_ids": ["sha256:" + "d" * 64]},
        }
        with (
            mock.patch.object(repro, "SUPPORTED_TARGETS", ("integrated",)),
            mock.patch.object(repro, "SUPPORTED_PLATFORMS", ("linux/amd64",)),
            mock.patch.object(repro, "remote_image_config", return_value=remote_config),
            mock.patch.object(repro, "build_platform_archive"),
            mock.patch.object(
                repro,
                "oci_image_config",
                return_value=("sha256:" + "1" * 64, local_config),
            ),
        ):
            with self.assertRaisesRegex(repro.ReproducibilityMismatch, "content/config"):
                repro.verify_reproducibility(
                    manifest={"build": {}},
                    image_release=self._single_platform_release(),
                    workspace_root=Path("/workspace"),
                )


if __name__ == "__main__":
    unittest.main()
