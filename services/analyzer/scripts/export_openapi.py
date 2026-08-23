"""Regenerate contracts/openapi.json from the running app.

The committed file is what frontend clients are generated from, so it is
verified against the live schema by tests/test_openapi_contract.py. Run this
whenever a route, response model, or error code changes.

    uv run python scripts/export_openapi.py
"""

from __future__ import annotations

import json
from pathlib import Path

from app.main import app

TARGET = Path(__file__).resolve().parents[3] / "contracts" / "openapi.json"


def main() -> None:
    app.openapi_schema = None
    schema = app.openapi()
    TARGET.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
    print(f"wrote {TARGET} ({len(schema['paths'])} paths)")


if __name__ == "__main__":
    main()
