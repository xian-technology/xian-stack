from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parents[2]
WORKSPACE_IMPORT_PATHS = (
    ROOT_DIR / "xian-py" / "src",
    ROOT_DIR / "xian-contracting" / "src",
    ROOT_DIR / "xian-contracting" / "packages" / "xian-accounts" / "src",
    ROOT_DIR / "xian-contracting" / "packages" / "xian-runtime-types" / "src",
    ROOT_DIR / "xian-contracting" / "packages" / "xian-zk" / "python",
    ROOT_DIR / "xian-abci" / "src",
)

# Native compiler and VM bindings use the non-editable builds declared in
# pyproject.toml; source paths could shadow them with stale development binaries.
for path in reversed(WORKSPACE_IMPORT_PATHS):
    if path.exists():
        sys.path.insert(0, str(path))


@pytest.fixture(autouse=True)
def isolate_stack_credentials(tmp_path, monkeypatch):
    """Backend and Make tests must never load the operator's real credentials."""
    monkeypatch.setenv("XIAN_STACK_SECRETS_ENV", str(tmp_path / "stack-secrets.env"))
