from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from export_python_runtime_requirements import export_requirements


def test_export_omits_editable_and_noneditable_local_packages(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "node"\nversion = "1.0.0"\n'
        'requires-python = ">=3.14"\ndependencies = ["compiler"]\n'
    )
    (tmp_path / "uv.lock").write_text(
        """version = 1
revision = 1
requires-python = ">=3.14"

[[package]]
name = "node"
version = "1.0.0"
source = { editable = "." }
dependencies = [{ name = "compiler" }]

[[package]]
name = "compiler"
version = "1.0.0"
source = { directory = "../compiler" }
dependencies = [{ name = "typing-extensions" }]

[[package]]
name = "typing-extensions"
version = "4.15.0"
source = { registry = "https://pypi.org/simple" }
[package.sdist]
url = "https://example.invalid/typing_extensions-4.15.0.tar.gz"
hash = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
"""
    )

    exported = export_requirements(tmp_path)
    requirements = [
        line for line in exported.splitlines() if line and not line.lstrip().startswith("#")
    ]
    assert requirements == [
        "typing-extensions==4.15.0 \\",
        "    --hash=sha256:" + "0" * 64,
    ]


def test_image_export_includes_sdk_crypto_and_rejects_conflicting_pins():
    import pytest
    from export_python_runtime_requirements import merge_requirements

    node = "cffi==2.0.0 \\\n    --hash=sha256:abc\n"
    sdk = node + "cryptography==48.0.1 \\\n    --hash=sha256:def\n"
    merged = merge_requirements(node, sdk)
    assert merged.count("cffi==2.0.0") == 1
    assert "cryptography==48.0.1" in merged
    assert "--hash=sha256:def" in merged
    with pytest.raises(ValueError, match="lock conflict"):
        merge_requirements(node, sdk.replace("cffi==2.0.0", "cffi==1.0.0"))
