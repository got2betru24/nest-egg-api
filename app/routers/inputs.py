# =============================================================================
# NestEgg - routers/inputs.py
# All scenario input data routes:
#   - Persons (create, update)
#   - Assumptions (upsert)
#   - Accounts (list, upsert)
#   - Contributions (upsert per account)
#   - Roth conversion overrides (list, upsert, delete)
#
# These routes complete the "no outside app activity required" goal — every
# input the user needs to set can be done through the API / UI.
# =============================================================================

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from ..database import execute, fetchall, fetchone
from ..models import (
    AccountCreate,
    AccountOut,
    AssumptionsCreate,
    AssumptionsOut,
    ContributionCreate,
    ContributionOut,
    MessageResponse,
    PersonCreate,
    PersonOut,
    RothConversionOverride,
    RothConversionOut,
)

router = APIRouter(tags=["inputs"])


# =============================================================================
# PERSONS
# =============================================================================


@router.post(
    "/scenarios/{scenario_id}/persons",
    response_model=PersonOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_person(scenario_id: int, body: PersonCreate):
    """Create a person (primary or spouse) for a scenario. Only one of each role allowed."""
    scenario = await fetchone(
        "SELECT id FROM scenarios WHERE id = %s AND is_active = 1", (scenario_id,)
    )
    if not scenario:
        raise HTTPException(status_code=404, detail="Scenario not found")

    existing = await fetchone(
        "SELECT id FROM persons WHERE scenario_id = %s AND role = %s",
        (scenario_id, body.role),
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"A person with role '{body.role}' already exists for this scenario. Use PATCH to update.",
        )

    person_id = await execute(
        """INSERT INTO persons (scenario_id, role, birth_year, birth_month, planned_retirement_age)
           VALUES (%s, %s, %s, %s, %s)""",
        (
            scenario_id,
            body.role,
            body.birth_year,
            body.birth_month or 1,
            body.planned_retirement_age,
        ),
    )
    row = await fetchone("SELECT * FROM persons WHERE id = %s", (person_id,))
    return row


@router.patch("/persons/{person_id}", response_model=PersonOut)
async def update_person(person_id: int, body: PersonCreate):
    """Update an existing person's details."""
    existing = await fetchone("SELECT id FROM persons WHERE id = %s", (person_id,))
    if not existing:
        raise HTTPException(status_code=404, detail="Person not found")

    await execute(
        """UPDATE persons
           SET birth_year = %s, birth_month = %s, planned_retirement_age = %s
           WHERE id = %s""",
        (
            body.birth_year,
            body.birth_month or 1,
            body.planned_retirement_age,
            person_id,
        ),
    )
    row = await fetchone("SELECT * FROM persons WHERE id = %s", (person_id,))
    return row


@router.get("/scenarios/{scenario_id}/persons", response_model=list[PersonOut])
async def list_persons(scenario_id: int):
    """List all persons for a scenario."""
    rows = await fetchall(
        "SELECT * FROM persons WHERE scenario_id = %s ORDER BY role", (scenario_id,)
    )
    return rows


@router.delete("/persons/{person_id}", response_model=MessageResponse)
async def delete_person(person_id: int):
    """Delete a person (and cascade: SS earnings, claiming)."""
    existing = await fetchone("SELECT id FROM persons WHERE id = %s", (person_id,))
    if not existing:
        raise HTTPException(status_code=404, detail="Person not found")
    await execute("DELETE FROM persons WHERE id = %s", (person_id,))
    return {"message": f"Person {person_id} deleted."}


# =============================================================================
# ASSUMPTIONS
# =============================================================================


@router.put("/scenarios/{scenario_id}/assumptions", response_model=AssumptionsOut)
async def upsert_assumptions(scenario_id: int, body: AssumptionsCreate):
    """Create or replace assumptions for a scenario."""
    scenario = await fetchone(
        "SELECT id FROM scenarios WHERE id = %s AND is_active = 1", (scenario_id,)
    )
    if not scenario:
        raise HTTPException(status_code=404, detail="Scenario not found")

    existing = await fetchone(
        "SELECT id FROM scenario_assumptions WHERE scenario_id = %s", (scenario_id,)
    )

    if existing:
        await execute(
            """UPDATE scenario_assumptions SET
               inflation_rate = %s, plan_to_age = %s, filing_status = %s,
               current_income = %s, desired_retirement_income = %s,
               healthcare_annual_cost = %s, enable_catchup_contributions = %s,
               enable_roth_ladder = %s, return_scenario = %s
               WHERE scenario_id = %s""",
            (
                body.inflation_rate,
                body.plan_to_age,
                body.filing_status.value,
                body.current_income,
                body.desired_retirement_income,
                body.healthcare_annual_cost,
                int(body.enable_catchup_contributions),
                int(body.enable_roth_ladder),
                body.return_scenario.value,
                scenario_id,
            ),
        )
    else:
        await execute(
            """INSERT INTO scenario_assumptions
               (scenario_id, inflation_rate, plan_to_age, filing_status,
                current_income, desired_retirement_income, healthcare_annual_cost,
                enable_catchup_contributions, enable_roth_ladder, return_scenario)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                scenario_id,
                body.inflation_rate,
                body.plan_to_age,
                body.filing_status.value,
                body.current_income,
                body.desired_retirement_income,
                body.healthcare_annual_cost,
                int(body.enable_catchup_contributions),
                int(body.enable_roth_ladder),
                body.return_scenario.value,
            ),
        )

    row = await fetchone(
        "SELECT * FROM scenario_assumptions WHERE scenario_id = %s", (scenario_id,)
    )
    return _coerce_assumptions(row)


@router.get("/scenarios/{scenario_id}/assumptions", response_model=AssumptionsOut)
async def get_assumptions(scenario_id: int):
    row = await fetchone(
        "SELECT * FROM scenario_assumptions WHERE scenario_id = %s", (scenario_id,)
    )
    if not row:
        raise HTTPException(
            status_code=404, detail="Assumptions not configured for this scenario"
        )
    return _coerce_assumptions(row)


def _coerce_assumptions(row: dict) -> dict:
    """Convert tinyint booleans from MySQL to Python bools."""
    return {
        **row,
        "enable_catchup_contributions": bool(row["enable_catchup_contributions"]),
        "enable_roth_ladder": bool(row["enable_roth_ladder"]),
    }


# =============================================================================
# ACCOUNTS
# =============================================================================


@router.get("/scenarios/{scenario_id}/accounts", response_model=list[AccountOut])
async def list_accounts(scenario_id: int):
    rows = await fetchall(
        "SELECT * FROM accounts WHERE scenario_id = %s ORDER BY account_type",
        (scenario_id,),
    )
    return rows


@router.put("/scenarios/{scenario_id}/accounts", response_model=AccountOut)
async def upsert_account(scenario_id: int, body: AccountCreate):
    """Create or update a single account by type. Each account_type is unique per scenario."""
    scenario = await fetchone(
        "SELECT id FROM scenarios WHERE id = %s AND is_active = 1", (scenario_id,)
    )
    if not scenario:
        raise HTTPException(status_code=404, detail="Scenario not found")

    existing = await fetchone(
        "SELECT id FROM accounts WHERE scenario_id = %s AND account_type = %s",
        (scenario_id, body.account_type.value),
    )

    if existing:
        await execute(
            """UPDATE accounts SET
               label = %s, current_balance = %s,
               return_conservative = %s, return_base = %s, return_optimistic = %s
               WHERE id = %s""",
            (
                body.label,
                body.current_balance,
                body.return_conservative,
                body.return_base,
                body.return_optimistic,
                existing["id"],
            ),
        )
        account_id = existing["id"]
    else:
        account_id = await execute(
            """INSERT INTO accounts
               (scenario_id, account_type, label, current_balance,
                return_conservative, return_base, return_optimistic)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (
                scenario_id,
                body.account_type.value,
                body.label,
                body.current_balance,
                body.return_conservative,
                body.return_base,
                body.return_optimistic,
            ),
        )

    row = await fetchone("SELECT * FROM accounts WHERE id = %s", (account_id,))
    return row


@router.put(
    "/scenarios/{scenario_id}/accounts/bulk",
    response_model=list[AccountOut],
)
async def upsert_accounts_bulk(scenario_id: int, body: list[AccountCreate]):
    """Upsert all five accounts in one request — used by the Inputs page on save."""
    scenario = await fetchone(
        "SELECT id FROM scenarios WHERE id = %s AND is_active = 1", (scenario_id,)
    )
    if not scenario:
        raise HTTPException(status_code=404, detail="Scenario not found")

    results = []
    for acct in body:
        existing = await fetchone(
            "SELECT id FROM accounts WHERE scenario_id = %s AND account_type = %s",
            (scenario_id, acct.account_type.value),
        )
        if existing:
            await execute(
                """UPDATE accounts SET
                   label = %s, current_balance = %s,
                   return_conservative = %s, return_base = %s, return_optimistic = %s
                   WHERE id = %s""",
                (
                    acct.label,
                    acct.current_balance,
                    acct.return_conservative,
                    acct.return_base,
                    acct.return_optimistic,
                    existing["id"],
                ),
            )
            account_id = existing["id"]
        else:
            account_id = await execute(
                """INSERT INTO accounts
                   (scenario_id, account_type, label, current_balance,
                    return_conservative, return_base, return_optimistic)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (
                    scenario_id,
                    acct.account_type.value,
                    acct.label,
                    acct.current_balance,
                    acct.return_conservative,
                    acct.return_base,
                    acct.return_optimistic,
                ),
            )
        row = await fetchone("SELECT * FROM accounts WHERE id = %s", (account_id,))
        results.append(row)

    return results


# =============================================================================
# CONTRIBUTIONS
# =============================================================================


@router.put("/accounts/{account_id}/contributions", response_model=ContributionOut)
async def upsert_contribution(account_id: int, body: ContributionCreate):
    """Create or update the contribution plan for an account."""
    account = await fetchone("SELECT id FROM accounts WHERE id = %s", (account_id,))
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    existing = await fetchone(
        "SELECT id FROM contributions WHERE account_id = %s", (account_id,)
    )
    if existing:
        await execute(
            """UPDATE contributions SET
               annual_amount = %s, employer_match_amount = %s,
               enforce_irs_limits = %s, solve_mode = %s
               WHERE account_id = %s""",
            (
                body.annual_amount,
                body.employer_match_amount,
                int(body.enforce_irs_limits),
                body.solve_mode.value,
                account_id,
            ),
        )
    else:
        await execute(
            """INSERT INTO contributions
               (account_id, annual_amount, employer_match_amount, enforce_irs_limits, solve_mode)
               VALUES (%s, %s, %s, %s, %s)""",
            (
                account_id,
                body.annual_amount,
                body.employer_match_amount,
                int(body.enforce_irs_limits),
                body.solve_mode.value,
            ),
        )

    row = await fetchone(
        "SELECT * FROM contributions WHERE account_id = %s", (account_id,)
    )
    return _coerce_contribution(row)


@router.get("/accounts/{account_id}/contributions", response_model=ContributionOut)
async def get_contribution(account_id: int):
    row = await fetchone(
        "SELECT * FROM contributions WHERE account_id = %s", (account_id,)
    )
    if not row:
        raise HTTPException(
            status_code=404, detail="No contributions configured for this account"
        )
    return _coerce_contribution(row)


def _coerce_contribution(row: dict) -> dict:
    return {**row, "enforce_irs_limits": bool(row["enforce_irs_limits"])}


# =============================================================================
# ROTH CONVERSION OVERRIDES
# =============================================================================


@router.get(
    "/scenarios/{scenario_id}/roth-conversions", response_model=list[RothConversionOut]
)
async def list_roth_conversions(scenario_id: int):
    rows = await fetchall(
        "SELECT * FROM roth_conversions WHERE scenario_id = %s ORDER BY plan_year",
        (scenario_id,),
    )
    return rows


@router.put(
    "/scenarios/{scenario_id}/roth-conversions", response_model=RothConversionOut
)
async def upsert_roth_conversion(scenario_id: int, body: RothConversionOverride):
    """Set a manual Roth conversion amount for a specific year."""
    scenario = await fetchone(
        "SELECT id FROM scenarios WHERE id = %s AND is_active = 1", (scenario_id,)
    )
    if not scenario:
        raise HTTPException(status_code=404, detail="Scenario not found")

    existing = await fetchone(
        "SELECT id FROM roth_conversions WHERE scenario_id = %s AND plan_year = %s",
        (scenario_id, body.plan_year),
    )
    if existing:
        await execute(
            """UPDATE roth_conversions
               SET amount = %s, is_optimizer_suggested = 0
               WHERE scenario_id = %s AND plan_year = %s""",
            (body.amount, scenario_id, body.plan_year),
        )
    else:
        await execute(
            """INSERT INTO roth_conversions
               (scenario_id, plan_year, amount, source_account, is_optimizer_suggested)
               VALUES (%s, %s, %s, %s, %s)""",
            (scenario_id, body.plan_year, body.amount, "traditional_401k", 0),
        )

    row = await fetchone(
        "SELECT * FROM roth_conversions WHERE scenario_id = %s AND plan_year = %s",
        (scenario_id, body.plan_year),
    )
    return row


@router.delete(
    "/scenarios/{scenario_id}/roth-conversions/{plan_year}",
    response_model=MessageResponse,
)
async def delete_roth_conversion(scenario_id: int, plan_year: int):
    existing = await fetchone(
        "SELECT id FROM roth_conversions WHERE scenario_id = %s AND plan_year = %s",
        (scenario_id, plan_year),
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Conversion override not found")
    await execute(
        "DELETE FROM roth_conversions WHERE scenario_id = %s AND plan_year = %s",
        (scenario_id, plan_year),
    )
    return {"message": f"Conversion override for {plan_year} deleted."}
