from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def test_stack_uses_installed_native_bindings_despite_stale_source_builds(tmp_path):
    tests_dir = tmp_path / "xian-stack" / "tests"
    tests_dir.mkdir(parents=True)
    conftest = tests_dir / "conftest.py"
    shutil.copyfile(Path(__file__).with_name("conftest.py"), conftest)

    for package in ("compiler", "vm"):
        native_source = (
            tmp_path
            / "xian-contracting"
            / "packages"
            / f"xian-{package}-core"
            / "python"
            / f"xian_{package}_core"
        )
        native_source.mkdir(parents=True)
        (native_source / "__init__.py").write_text(
            'raise RuntimeError("stale in-tree native build loaded")\n'
        )

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            """
import runpy
import sys

runpy.run_path(sys.argv[1])
from xian_compiler_core import compiler_version
from xian_vm_core import runtime_info

assert compiler_version()
assert runtime_info()["vm_profile"] == "xian_vm_v2"
""",
            str(conftest),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
