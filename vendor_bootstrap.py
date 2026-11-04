from __future__ import annotations

import sys
from pathlib import Path

VENDOR_DIR = Path(__file__).resolve().parent / "vendor"


def ensure_vendor() -> None:
    path = str(VENDOR_DIR)
    if path not in sys.path:
        sys.path.insert(0, path)
