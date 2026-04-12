# =============================================================================
# NestEgg - routers/projection.py
# Runs the year-by-year projection engine and returns/caches results.
# =============================================================================

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..database import execute, executemany, fetchall, fetchone
from ..models import ProjectionRequest, ProjectionResultOut
from ..utils import current_year
from ..engine.projection import (
    AccountInputs,
    ContributionInputs,
    PersonInputs,
    ProjectionInputs,
    ReturnScenario,
    run_projection,
)
from ..engine.tax_engine import BracketRow, TaxYear
from ..engine.contribution_limits import LimitRow
from ..engine.social_security import (
    EarningsRecord,
    BendPointRow,
    AWIRow,
    FRARule,
    estimate_benefit,
)

router = APIRouter(prefix="/projection", tags=["projection"])


@router.post("/run", response_model=ProjectionResultOut)
async def run_projection_endpoint(body: ProjectionRequest):
    scenario = await fetchone(
        "SELECT id FROM scenarios WHERE id = %s AND is_active = 1", (body.scenario_id,)
    )
    if not scenario:
        raise HTTPException(status_code=404, detail="Scenario not found")

    # Check cache
    if not body.force_recompute:
        cached = await _load_cache(body.scenario_id, body.return_scenario.value)
        if cached:
            return cached

    inputs = await _build_projection_inputs(body.scenario_id, body.return_scenario)
    result = run_projection(inputs, ReturnScenario(body.return_scenario.value))

    # Save to cache
    await _save_cache(body.scenario_id, body.return_scenario.value, result)

    return _result_to_out(body.scenario_id, body.return_scenario, result)


@router.delete("/{scenario_id}/cache")
async def invalidate_cache(scenario_id: int):
    await execute("DELETE FROM projection_cache WHERE scenario_id = %s", (scenario_id,))
    return {"message": "Cache invalidated."}


# ---------------------------------------------------------------------------
# Build engine inputs from DB
# ---------------------------------------------------------------------------


async def _build_projection_inputs(
    scenario_id: int,
    return_scenario,
) -> ProjectionInputs:
    assumptions = await fetchone(
        "SELECT * FROM scenario_assumptions WHERE scenario_id = %s", (scenario_id,)
    )
    if not assumptions:
        raise HTTPException(
            status_code=422, detail="Scenario assumptions not configured"
        )

    persons = await fetchall(
        "SELECT * FROM persons WHERE scenario_id = %s", (scenario_id,)
    )
    primary_row = next((p for p in persons if p["role"] == "primary"), None)
    spouse_row = next((p for p in persons if p["role"] == "spouse"), None)
    if not primary_row:
        raise HTTPException(status_code=422, detail="Primary person not configured")

    accounts = await fetchall(
        "SELECT * FROM accounts WHERE scenario_id = %s", (scenario_id,)
    )
    contribs_rows = []
    if accounts:
        ids = tuple(a["id"] for a in accounts)
        placeholders = ",".join(["%s"] * len(ids))
        contribs_rows = await fetchall(
            f"SELECT * FROM contributions WHERE account_id IN ({placeholders})", ids
        )

    def get_account(atype: str) -> dict | None:
        return next((a for a in accounts if a["account_type"] == atype), None)

    def get_contrib(account_id: int) -> dict | None:
        return next((c for c in contribs_rows if c["account_id"] == account_id), None)

    # Accounts
    hysa = get_account("hysa") or {}
    brokerage = get_account("brokerage") or {}
    roth_ira = get_account("roth_ira") or {}
    trad_401k = get_account("traditional_401k") or {}
    roth_401k = get_account("roth_401k") or {}

    rs = return_scenario.value

    account_inputs = AccountInputs(
        hysa_balance=hysa.get("current_balance", 0),
        brokerage_balance=brokerage.get("current_balance", 0),
        roth_ira_balance=roth_ira.get("current_balance", 0),
        traditional_401k_balance=trad_401k.get("current_balance", 0),
        roth_401k_balance=roth_401k.get("current_balance", 0),
        hysa_return_conservative=hysa.get("return_conservative", 0.02),
        hysa_return_base=hysa.get("return_base", 0.04),
        hysa_return_optimistic=hysa.get("return_optimistic", 0.05),
        brokerage_return_conservative=brokerage.get("return_conservative", 0.05),
        brokerage_return_base=brokerage.get("return_base", 0.07),
        brokerage_return_optimistic=brokerage.get("return_optimistic", 0.10),
        roth_ira_return_conservative=roth_ira.get("return_conservative", 0.05),
        roth_ira_return_base=roth_ira.get("return_base", 0.07),
        roth_ira_return_optimistic=roth_ira.get("return_optimistic", 0.10),
        traditional_401k_return_conservative=trad_401k.get("return_conservative", 0.05),
        traditional_401k_return_base=trad_401k.get("return_base", 0.07),
        traditional_401k_return_optimistic=trad_401k.get("return_optimistic", 0.10),
        roth_401k_return_conservative=roth_401k.get("return_conservative", 0.05),
        roth_401k_return_base=roth_401k.get("return_base", 0.07),
        roth_401k_return_optimistic=roth_401k.get("return_optimistic", 0.10),
    )

    # Contributions (from 401k account)
    trad_401k_contrib = get_contrib(trad_401k["id"]) if trad_401k.get("id") else None
    roth_401k_contrib = get_contrib(roth_401k["id"]) if roth_401k.get("id") else None
    roth_ira_contrib = get_contrib(roth_ira["id"]) if roth_ira.get("id") else None

    contribution_inputs = ContributionInputs(
        traditional_401k_annual=trad_401k_contrib["annual_amount"]
        if trad_401k_contrib
        else 0,
        roth_401k_annual=roth_401k_contrib["annual_amount"] if roth_401k_contrib else 0,
        roth_ira_annual=roth_ira_contrib["annual_amount"] if roth_ira_contrib else 0,
        employer_match_annual=trad_401k_contrib["employer_match_amount"]
        if trad_401k_contrib
        else 0,
        enable_catchup=bool(assumptions["enable_catchup_contributions"]),
    )

    # Resolve the best available tax year — fall back to the most recent
    # seeded year if the current calendar year isn't in the DB yet.
    effective_year_row = await fetchone(
        "SELECT MAX(tax_year) AS yr FROM contribution_limits"
    )
    effective_year: int = (
        effective_year_row["yr"]
        if effective_year_row and effective_year_row["yr"]
        else current_year()
    )

    # IRS contribution limits
    limit_rows_raw = await fetchall(
        "SELECT account_type, limit_type, amount, catchup_age "
        "FROM contribution_limits WHERE tax_year = %s",
        (effective_year,),
    )
    limit_rows = [LimitRow(**r) for r in limit_rows_raw]

    # Tax brackets — use same effective year, fallback already mirrors tax.py
    tax_brackets_raw = await fetchall(
        "SELECT rate, income_min, income_max FROM tax_brackets "
        "WHERE tax_year = %s AND filing_status = %s",
        (effective_year, "married_filing_jointly"),
    )
    if not tax_brackets_raw:
        # tax_brackets may be seeded for a different year than contribution_limits
        latest_bracket_row = await fetchone(
            "SELECT MAX(tax_year) AS yr FROM tax_brackets "
            "WHERE filing_status = 'married_filing_jointly'"
        )
        bracket_year: int = (
            latest_bracket_row["yr"]
            if latest_bracket_row and latest_bracket_row["yr"]
            else effective_year
        )
        tax_brackets_raw = await fetchall(
            "SELECT rate, income_min, income_max FROM tax_brackets "
            "WHERE tax_year = %s AND filing_status = %s",
            (bracket_year, "married_filing_jointly"),
        )
    else:
        bracket_year = effective_year

    std_deduction_row = await fetchone(
        "SELECT amount FROM standard_deductions WHERE tax_year = %s AND filing_status = %s",
        (bracket_year, "married_filing_jointly"),
    )
    std_deduction = std_deduction_row["amount"] if std_deduction_row else 30000.0

    tax_year_obj = TaxYear(
        year=bracket_year,
        brackets=[
            BracketRow(
                rate=r["rate"],
                income_min=r["income_min"],
                income_max=r["income_max"],
            )
            for r in tax_brackets_raw
        ],
        standard_deduction=std_deduction,
    )

    # Roth overrides
    roth_overrides_raw = await fetchall(
        "SELECT plan_year, amount FROM roth_conversions WHERE scenario_id = %s",
        (scenario_id,),
    )
    roth_overrides = {r["plan_year"]: r["amount"] for r in roth_overrides_raw}

    # SS for primary
    primary_ss = await _build_ss_person_inputs(primary_row, assumptions)
    spouse_ss = (
        await _build_ss_person_inputs(spouse_row, assumptions) if spouse_row else None
    )

    return ProjectionInputs(
        current_year=current_year(),
        primary=primary_ss,
        spouse=spouse_ss,
        accounts=account_inputs,
        contributions=contribution_inputs,
        limit_rows=limit_rows,
        desired_retirement_income_today=assumptions["desired_retirement_income"],
        current_income=assumptions["current_income"],
        inflation_rate=assumptions["inflation_rate"],
        plan_to_age=assumptions["plan_to_age"],
        healthcare_annual_cost=assumptions["healthcare_annual_cost"],
        tax_years={current_year(): tax_year_obj},
        enable_roth_ladder=bool(assumptions["enable_roth_ladder"]),
        roth_ladder_overrides=roth_overrides,
    )


async def _build_ss_person_inputs(person_row: dict, assumptions: dict) -> PersonInputs:
    from ..engine.projection import PersonInputs as PI

    person_id = person_row["id"]

    claiming = await fetchone(
        "SELECT * FROM ss_claiming WHERE person_id = %s", (person_id,)
    )
    if not claiming:
        return PI(
            birth_year=person_row["birth_year"],
            retirement_age=person_row["planned_retirement_age"],
            ss_benefit=None,
            ss_claim_start_year=None,
        )

    earnings_rows = await fetchall(
        "SELECT earn_year, earnings FROM ss_earnings WHERE person_id = %s", (person_id,)
    )
    earnings = [
        EarningsRecord(year=r["earn_year"], earnings=r["earnings"])
        for r in earnings_rows
    ]

    benefit_year = person_row["birth_year"] + 62
    bend_row = await fetchone(
        "SELECT * FROM ss_bend_points WHERE benefit_year <= %s ORDER BY benefit_year DESC LIMIT 1",
        (benefit_year,),
    )
    awi_rows = await fetchall(
        "SELECT awi_year, awi_value FROM ss_awi ORDER BY awi_year"
    )
    fra_rows = await fetchall("SELECT * FROM ss_fra")

    ss_benefit = estimate_benefit(
        birth_year=person_row["birth_year"],
        claim_age_years=claiming["claim_age_years"],
        claim_age_months=claiming["claim_age_months"],
        earnings_records=earnings,
        awi_rows=[
            AWIRow(year=r["awi_year"], awi_value=r["awi_value"]) for r in awi_rows
        ],
        bend_point_row=BendPointRow(
            **{
                k: bend_row[k]
                for k in [
                    "benefit_year",
                    "bend_point_1",
                    "bend_point_2",
                    "factor_below_1",
                    "factor_1_to_2",
                    "factor_above_2",
                ]
            }
        ),
        fra_rules=[
            FRARule(
                **{
                    k: r[k]
                    for k in [
                        "birth_year_min",
                        "birth_year_max",
                        "fra_years",
                        "fra_months",
                    ]
                }
            )
            for r in fra_rows
        ],
        assumed_future_income=assumptions["current_income"],
        current_age=current_year() - person_row["birth_year"],
        retirement_age=person_row["planned_retirement_age"],
    )

    cola_rows = await fetchall("SELECT cola_year, rate FROM ss_cola ORDER BY cola_year")

    return PI(
        birth_year=person_row["birth_year"],
        retirement_age=person_row["planned_retirement_age"],
        ss_benefit=ss_benefit,
        ss_claim_start_year=person_row["birth_year"] + claiming["claim_age_years"],
        ss_cola_rows=[
            {"cola_year": r["cola_year"], "rate": r["rate"]} for r in cola_rows
        ],
    )


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------


async def _load_cache(
    scenario_id: int, return_scenario: str
) -> ProjectionResultOut | None:
    rows = await fetchall(
        "SELECT * FROM projection_cache WHERE scenario_id = %s AND return_scenario = %s "
        "ORDER BY plan_year",
        (scenario_id, return_scenario),
    )
    if not rows:
        return None
    # Build minimal result from cache rows
    # (Full rebuilding — cache is mainly for repeated reads, not complex reconstruction)
    return None  # Simplified: always recompute for now; extend in v2


async def _save_cache(scenario_id: int, return_scenario: str, result) -> None:
    # Clear existing cache for this scenario/scenario
    await execute(
        "DELETE FROM projection_cache WHERE scenario_id = %s AND return_scenario = %s",
        (scenario_id, return_scenario),
    )
    rows = []
    for yr in result.years:
        rows.append(
            (
                scenario_id,
                return_scenario,
                yr.calendar_year,
                yr.age_primary,
                yr.age_spouse,
                yr.balances_end.hysa,
                yr.balances_end.brokerage,
                yr.balances_end.roth_ira,
                yr.balances_end.traditional_401k,
                yr.balances_end.roth_401k,
                yr.balances_end.total_pretax,
                yr.balances_end.total_posttax,
                yr.balances_end.total,
                yr.gross_income,
                yr.ss_primary,
                yr.ss_spouse,
                yr.tax_result.ordinary.taxable_income if yr.tax_result else 0,
                yr.tax_result.total_tax if yr.tax_result else 0,
                yr.tax_result.total_effective_rate if yr.tax_result else 0,
                yr.withdrawals.hysa,
                yr.withdrawals.brokerage,
                yr.withdrawals.roth_ira,
                yr.withdrawals.traditional_401k,
                yr.withdrawals.roth_401k,
                yr.withdrawals.roth_conversion,
                yr.contributions.traditional_401k,
                yr.contributions.roth_401k,
                yr.contributions.roth_ira,
            )
        )

    if rows:
        await executemany(
            """INSERT INTO projection_cache (
               scenario_id, return_scenario, plan_year, age_primary, age_spouse,
               bal_hysa, bal_brokerage, bal_roth_ira, bal_traditional_401k, bal_roth_401k,
               bal_total_pretax, bal_total_posttax, bal_total,
               gross_income, ss_income_primary, ss_income_spouse,
               taxable_income, tax_owed, effective_tax_rate,
               withdrawal_hysa, withdrawal_brokerage, withdrawal_roth_ira,
               withdrawal_401k, withdrawal_roth_401k, roth_conversion_amount,
               contrib_401k, contrib_roth_401k, contrib_roth_ira
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                      %s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            rows,
        )


# ---------------------------------------------------------------------------
# Result serialization
# ---------------------------------------------------------------------------


def _result_to_out(scenario_id: int, return_scenario, result) -> ProjectionResultOut:
    years_out = []
    for yr in result.years:
        tax_out = None
        if yr.tax_result:
            t = yr.tax_result
            tax_out = {
                "ordinary_taxable_income": t.ordinary.taxable_income,
                "standard_deduction": t.ordinary.standard_deduction,
                "tax_owed": t.ordinary.tax_owed,
                "effective_rate": t.ordinary.effective_rate,
                "marginal_rate": t.ordinary.marginal_rate,
                "ltcg_tax": t.ltcg.ltcg_tax,
                "niit": t.ltcg.niit,
                "ss_taxable_amount": t.ss_taxable_amount,
                "total_tax": t.total_tax,
                "total_effective_rate": t.total_effective_rate,
            }

        years_out.append(
            {
                "calendar_year": yr.calendar_year,
                "age_primary": yr.age_primary,
                "age_spouse": yr.age_spouse,
                "phase": yr.phase.value,
                "balances_start": _bal_to_dict(yr.balances_start),
                "balances_end": _bal_to_dict(yr.balances_end),
                "contributions": {
                    "traditional_401k": yr.contributions.traditional_401k,
                    "roth_401k": yr.contributions.roth_401k,
                    "roth_ira": yr.contributions.roth_ira,
                    "employer_match": yr.contributions.employer_match,
                },
                "withdrawals": {
                    "hysa": yr.withdrawals.hysa,
                    "brokerage": yr.withdrawals.brokerage,
                    "roth_ira": yr.withdrawals.roth_ira,
                    "traditional_401k": yr.withdrawals.traditional_401k,
                    "roth_401k": yr.withdrawals.roth_401k,
                    "roth_conversion": yr.withdrawals.roth_conversion,
                },
                "ss_primary": yr.ss_primary,
                "ss_spouse": yr.ss_spouse,
                "healthcare_cost": yr.healthcare_cost,
                "tax": tax_out,
                "gross_income": yr.gross_income,
                "net_income": yr.net_income,
                "income_target": yr.income_target,
                "income_gap": yr.income_gap,
                "roth_ladder_conversion": yr.roth_ladder_conversion,
                "roth_available_principal": yr.roth_available_principal,
                "is_depleted": yr.is_depleted,
                "notes": yr.notes,
            }
        )

    return {
        "scenario_id": scenario_id,
        "return_scenario": return_scenario,
        "years": years_out,
        "depletion_year": result.depletion_year,
        "depletion_age": result.depletion_age,
        "final_balance": result.final_balance,
        "total_tax_paid": result.total_tax_paid,
        "total_ss_received": result.total_ss_received,
        "success": result.success,
    }


def _bal_to_dict(b) -> dict:
    return {
        "hysa": b.hysa,
        "brokerage": b.brokerage,
        "roth_ira": b.roth_ira,
        "traditional_401k": b.traditional_401k,
        "roth_401k": b.roth_401k,
        "total_pretax": b.total_pretax,
        "total_posttax": b.total_posttax,
        "total": b.total,
    }
