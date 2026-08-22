#!/usr/bin/env python
"""Write the service's OpenAPI document to contracts/openapi.json.

The contracts directory is the boundary other services and the frontend import
from. Regenerate it whenever a request or response model changes.

Usage:
    cd services/analyzer && uv run python ../../scripts/export_openapi.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVICE_ROOT = REPO_ROOT / "services" / "analyzer"
OUTPUT = REPO_ROOT / "contracts" / "openapi.json"


def main() -> int:
    sys.path.insert(0, str(SERVICE_ROOT))
    from app.main import app

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"wrote {OUTPUT.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
