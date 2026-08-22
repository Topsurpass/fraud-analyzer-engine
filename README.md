# fraud-analyzer-engine

Backend for turning saved SQL into chart-ready JSON, against any database
schema.

The service lives in [`services/analyzer/`](services/analyzer/README.md). Start
there for setup, the safety model, and the API.

```bash
cd services/analyzer
uv venv --python 3.13 && uv pip install -e ".[dev]"
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

| Path | What it holds |
|---|---|
| `services/analyzer/` | The service: API, SQL guard, migrations, tests |
| `contracts/` | Frozen response shapes and the generated `openapi.json` |
| `scripts/` | `export_openapi.py` |
| `docs/superpowers/` | Design spec and implementation plan |

**Before you connect a production database, read the read-only role section in
[`services/analyzer/README.md`](services/analyzer/README.md#use-a-read-only-database-role).**
The service blocks writes at three layers, but a read-only database role is the
control that still holds if the service has a bug.
