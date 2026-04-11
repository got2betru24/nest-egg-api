# NestEgg — Backend API

FastAPI + Python 3.13 backend for the NestEgg retirement planning calculator.

## Stack
- **FastAPI** — async REST API
- **aiomysql** — async MySQL driver
- **Pydantic v2** — request/response validation
- **Uvicorn** — ASGI server

## Project layout

```
app/
  main.py           # FastAPI app, middleware, router registration
  database.py       # MySQL connection pool and context managers
  models.py         # Pydantic request/response models
  utils.py          # Shared helpers (CSV parsing, date math)
  engine/           # Pure financial math — no FastAPI/DB dependencies
    inflation.py
    contribution_limits.py
    tax_engine.py
    social_security.py
    bridge_strategy.py
    roth_ladder.py
    projection.py
    optimizer.py
  routers/
    scenarios.py        # Scenario CRUD
    projection.py       # Run projection engine
    optimizer.py        # Run optimizer
    social_security.py  # SS earnings upload + benefit estimation
    tax.py              # Tax bracket data + Roth conversion modeling
database/
  01_schema.sql     # Tables and indexes
  02_seed.sql       # Tax brackets, SS bend points, contribution limits
scripts/
  db_init.sh        # Initialize and seed the database
```

## Local development

1. Copy `.env.example` to `.env` and fill in your MySQL credentials.
2. Initialize the database:
   ```bash
   chmod +x scripts/db_init.sh
   ./scripts/db_init.sh
   ```
3. Start the API:
   ```bash
   docker compose up --build
   ```
   The API will be available at `http://localhost:8000`.
   Interactive docs: `http://localhost:8000/docs`

## API overview

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/scenarios/` | List all scenarios |
| POST | `/api/v1/scenarios/` | Create scenario |
| GET | `/api/v1/scenarios/{id}/full` | Load full scenario |
| POST | `/api/v1/scenarios/{id}/duplicate` | Duplicate scenario |
| POST | `/api/v1/projection/run` | Run year-by-year projection |
| POST | `/api/v1/optimizer/run` | Run strategy optimizer |
| POST | `/api/v1/social-security/earnings/{id}/upload` | Upload SS earnings CSV |
| GET | `/api/v1/social-security/comparison/{id}` | Early/FRA/late SS comparison |
| GET | `/api/v1/tax/brackets` | Current tax brackets |
| POST | `/api/v1/tax/estimate` | Estimate tax for income mix |
| POST | `/api/v1/tax/roth-conversion-cost` | Price a Roth conversion |

## Annual maintenance

Update `02_seed.sql` each year with:
- New IRS tax brackets and standard deductions
- New IRS contribution limits (401k, IRA catch-up)
- New SS bend points and AWI value
- New SS COLA rate

Then re-run `./scripts/db_init.sh` (uses `ON DUPLICATE KEY UPDATE` — safe to re-run).
