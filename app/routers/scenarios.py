# =============================================================================
# NestEgg - routers/scenarios.py
# Scenario CRUD: create, list, load full scenario, delete, duplicate.
# A "scenario" is the top-level container for all planning inputs.
# =============================================================================

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from ..database import execute, fetchall, fetchone
from ..models import (
    FullScenarioOut,
    MessageResponse,
    ScenarioCreate,
    ScenarioOut,
    ScenarioUpdate,
)

router = APIRouter(prefix="/scenarios", tags=["scenarios"])


# ---------------------------------------------------------------------------
# List all scenarios
# ---------------------------------------------------------------------------

@router.get("/", response_model=list[ScenarioOut])
async def list_scenarios():
    rows = await fetchall(
        "SELECT id, name, description, created_at, updated_at, is_active "
        "FROM scenarios WHERE is_active = 1 ORDER BY updated_at DESC"
    )
    return [_format_scenario(r) for r in rows]


# ---------------------------------------------------------------------------
# Create a scenario
# ---------------------------------------------------------------------------

@router.post("/", response_model=ScenarioOut, status_code=status.HTTP_201_CREATED)
async def create_scenario(body: ScenarioCreate):
    scenario_id = await execute(
        "INSERT INTO scenarios (name, description) VALUES (%s, %s)",
        (body.name, body.description),
    )
    row = await fetchone("SELECT * FROM scenarios WHERE id = %s", (scenario_id,))
    return _format_scenario(row)


# ---------------------------------------------------------------------------
# Get one scenario (header only)
# ---------------------------------------------------------------------------

@router.get("/{scenario_id}", response_model=ScenarioOut)
async def get_scenario(scenario_id: int):
    row = await fetchone(
        "SELECT * FROM scenarios WHERE id = %s AND is_active = 1", (scenario_id,)
    )
    if not row:
        raise HTTPException(status_code=404, detail="Scenario not found")
    return _format_scenario(row)


# ---------------------------------------------------------------------------
# Update scenario name/description
# ---------------------------------------------------------------------------

@router.patch("/{scenario_id}", response_model=ScenarioOut)
async def update_scenario(scenario_id: int, body: ScenarioUpdate):
    existing = await fetchone(
        "SELECT id FROM scenarios WHERE id = %s AND is_active = 1", (scenario_id,)
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Scenario not found")

    updates = {}
    if body.name is not None:
        updates["name"] = body.name
    if body.description is not None:
        updates["description"] = body.description

    if updates:
        set_clause = ", ".join(f"{k} = %s" for k in updates)
        await execute(
            f"UPDATE scenarios SET {set_clause} WHERE id = %s",
            (*updates.values(), scenario_id),
        )

    row = await fetchone("SELECT * FROM scenarios WHERE id = %s", (scenario_id,))
    return _format_scenario(row)


# ---------------------------------------------------------------------------
# Soft-delete a scenario
# ---------------------------------------------------------------------------

@router.delete("/{scenario_id}", response_model=MessageResponse)
async def delete_scenario(scenario_id: int):
    existing = await fetchone(
        "SELECT id FROM scenarios WHERE id = %s AND is_active = 1", (scenario_id,)
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Scenario not found")

    await execute(
        "UPDATE scenarios SET is_active = 0 WHERE id = %s", (scenario_id,)
    )
    return {"message": f"Scenario {scenario_id} deleted."}


# ---------------------------------------------------------------------------
# Duplicate a scenario (deep copy)
# ---------------------------------------------------------------------------

@router.post("/{scenario_id}/duplicate", response_model=ScenarioOut, status_code=status.HTTP_201_CREATED)
async def duplicate_scenario(scenario_id: int):
    source = await fetchone(
        "SELECT * FROM scenarios WHERE id = %s AND is_active = 1", (scenario_id,)
    )
    if not source:
        raise HTTPException(status_code=404, detail="Scenario not found")

    new_id = await execute(
        "INSERT INTO scenarios (name, description) VALUES (%s, %s)",
        (f"{source['name']} (copy)", source["description"]),
    )

    # Copy assumptions
    assumptions = await fetchone(
        "SELECT * FROM scenario_assumptions WHERE scenario_id = %s", (scenario_id,)
    )
    if assumptions:
        await execute(
            """INSERT INTO scenario_assumptions
               (scenario_id, inflation_rate, plan_to_age, filing_status,
                current_income, desired_retirement_income, healthcare_annual_cost,
                enable_catchup_contributions, enable_roth_ladder, return_scenario)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (new_id, assumptions["inflation_rate"], assumptions["plan_to_age"],
             assumptions["filing_status"], assumptions["current_income"],
             assumptions["desired_retirement_income"], assumptions["healthcare_annual_cost"],
             assumptions["enable_catchup_contributions"], assumptions["enable_roth_ladder"],
             assumptions["return_scenario"]),
        )

    # Copy persons (and their SS earnings / claiming)
    persons = await fetchall(
        "SELECT * FROM persons WHERE scenario_id = %s", (scenario_id,)
    )
    person_id_map: dict[int, int] = {}
    for p in persons:
        new_person_id = await execute(
            """INSERT INTO persons (scenario_id, role, birth_year, birth_month, planned_retirement_age)
               VALUES (%s,%s,%s,%s,%s)""",
            (new_id, p["role"], p["birth_year"], p["birth_month"], p["planned_retirement_age"]),
        )
        person_id_map[p["id"]] = new_person_id

        # Copy SS earnings
        earnings = await fetchall(
            "SELECT earn_year, earnings FROM ss_earnings WHERE person_id = %s", (p["id"],)
        )
        if earnings:
            await _bulk_insert_earnings(new_person_id, earnings)

        # Copy SS claiming
        claiming = await fetchone(
            "SELECT * FROM ss_claiming WHERE person_id = %s", (p["id"],)
        )
        if claiming:
            await execute(
                """INSERT INTO ss_claiming
                   (person_id, claim_age_years, claim_age_months, use_spousal_benefit, spousal_benefit_pct)
                   VALUES (%s,%s,%s,%s,%s)""",
                (new_person_id, claiming["claim_age_years"], claiming["claim_age_months"],
                 claiming["use_spousal_benefit"], claiming["spousal_benefit_pct"]),
            )

    # Copy accounts and contributions
    accounts = await fetchall(
        "SELECT * FROM accounts WHERE scenario_id = %s", (scenario_id,)
    )
    for acct in accounts:
        new_acct_id = await execute(
            """INSERT INTO accounts
               (scenario_id, account_type, label, current_balance,
                return_conservative, return_base, return_optimistic)
               VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (new_id, acct["account_type"], acct["label"], acct["current_balance"],
             acct["return_conservative"], acct["return_base"], acct["return_optimistic"]),
        )
        contrib = await fetchone(
            "SELECT * FROM contributions WHERE account_id = %s", (acct["id"],)
        )
        if contrib:
            await execute(
                """INSERT INTO contributions
                   (account_id, annual_amount, employer_match_amount, enforce_irs_limits, solve_mode)
                   VALUES (%s,%s,%s,%s,%s)""",
                (new_acct_id, contrib["annual_amount"], contrib["employer_match_amount"],
                 contrib["enforce_irs_limits"], contrib["solve_mode"]),
            )

    # Copy Roth conversion overrides
    conversions = await fetchall(
        "SELECT * FROM roth_conversions WHERE scenario_id = %s", (scenario_id,)
    )
    for conv in conversions:
        await execute(
            """INSERT INTO roth_conversions
               (scenario_id, plan_year, amount, source_account, is_optimizer_suggested)
               VALUES (%s,%s,%s,%s,%s)""",
            (new_id, conv["plan_year"], conv["amount"],
             conv["source_account"], conv["is_optimizer_suggested"]),
        )

    row = await fetchone("SELECT * FROM scenarios WHERE id = %s", (new_id,))
    return _format_scenario(row)


# ---------------------------------------------------------------------------
# Full scenario load (all related data)
# ---------------------------------------------------------------------------

@router.get("/{scenario_id}/full", response_model=FullScenarioOut)
async def get_full_scenario(scenario_id: int):
    scenario = await fetchone(
        "SELECT * FROM scenarios WHERE id = %s AND is_active = 1", (scenario_id,)
    )
    if not scenario:
        raise HTTPException(status_code=404, detail="Scenario not found")

    assumptions = await fetchone(
        "SELECT * FROM scenario_assumptions WHERE scenario_id = %s", (scenario_id,)
    )
    persons = await fetchall(
        "SELECT * FROM persons WHERE scenario_id = %s", (scenario_id,)
    )
    accounts = await fetchall(
        "SELECT * FROM accounts WHERE scenario_id = %s", (scenario_id,)
    )
    account_ids = [a["id"] for a in accounts]
    contributions = []
    if account_ids:
        placeholders = ",".join(["%s"] * len(account_ids))
        contributions = await fetchall(
            f"SELECT * FROM contributions WHERE account_id IN ({placeholders})",
            tuple(account_ids),
        )
    person_ids = [p["id"] for p in persons]
    ss_claiming = []
    if person_ids:
        placeholders = ",".join(["%s"] * len(person_ids))
        ss_claiming = await fetchall(
            f"SELECT * FROM ss_claiming WHERE person_id IN ({placeholders})",
            tuple(person_ids),
        )
    roth_overrides = await fetchall(
        "SELECT * FROM roth_conversions WHERE scenario_id = %s", (scenario_id,)
    )

    return {
        "scenario": _format_scenario(scenario),
        "assumptions": assumptions,
        "persons": persons,
        "accounts": accounts,
        "contributions": contributions,
        "ss_claiming": ss_claiming,
        "roth_overrides": roth_overrides,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_scenario(row: dict) -> dict:
    return {
        **row,
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


async def _bulk_insert_earnings(person_id: int, earnings: list[dict]) -> None:
    from ..database import executemany
    await executemany(
        "INSERT INTO ss_earnings (person_id, earn_year, earnings) VALUES (%s, %s, %s)",
        [(person_id, e["earn_year"], e["earnings"]) for e in earnings],
    )
