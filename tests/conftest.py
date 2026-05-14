from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
WORKSPACE_IMPORT_PATHS = (
    ROOT_DIR / "xian-py" / "src",
    ROOT_DIR / "xian-contracting" / "src",
    ROOT_DIR / "xian-contracting" / "packages" / "xian-accounts" / "src",
    ROOT_DIR / "xian-contracting" / "packages" / "xian-runtime-types" / "src",
    ROOT_DIR / "xian-contracting" / "packages" / "xian-vm-core" / "python",
    ROOT_DIR / "xian-contracting" / "packages" / "xian-zk" / "python",
    ROOT_DIR / "xian-abci" / "src",
)

for path in reversed(WORKSPACE_IMPORT_PATHS):
    if path.exists():
        sys.path.insert(0, str(path))
