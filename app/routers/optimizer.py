# =============================================================================
# NestEgg - routers/optimizer.py
# Runs the optimizer and returns the recommended strategy.
# =============================================================================

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..database import fetchall, fetchone
from ..models import OptimizerRequest, OptimizedStrategyOut
from ..engine.optimizer import OptimizerConfig, SSClaimingOption, run_optimizer
from ..engine.social_security import (
    AWIRow,
    BendPointRow,
    EarningsRecord,
    FRARule,
    estimate_benefit,
)
from .projection import _build_projection_inputs, _result_to_out

router = APIRouter(prefix="/optimizer", tags=["optimizer"])


@router.post("/run", response_model=OptimizedStrategyOut)
async def run_optimizer_endpoint(body: OptimizerRequest):
    scenario = await fetchone(
        "SELECT id FROM scenarios WHERE id = %s AND is_active = 1", (body.scenario_id,)
    )
    if not scenario:
        raise HTTPException(status_code=404, detail="Scenario not found")

    # Build base projection inputs
    base_inputs = await _build_projection_inputs(
        body.scenario_id, body.optimize_against_scenario
    )

    # Load persons for SS calculation
    persons = await fetchall(
        "SELECT * FROM persons WHERE scenario_id = %s", (body.scenario_id,)
    )
    primary_row = next((p for p in persons if p["role"] == "primary"), None)
    spouse_row = next((p for p in persons if p["role"] == "spouse"), None)
    if not primary_row:
        raise HTTPException(status_code=422, detail="Primary person not configured")

    # Load primary SS reference data (bend points keyed to primary's birth year)
    primary_earnings, bend_row, awi_rows, fra_rows = await _load_ss_reference(
        primary_row["id"], primary_row["birth_year"]
    )

    spouse_earnings = None
    spouse_use_spousal = True
    spouse_birth_year = None

    if spouse_row:
        spouse_birth_year = spouse_row["birth_year"]

        # Auto-determine whether the spouse is better off on their own record
        # or on the spousal benefit (50% of primary PIA at FRA). This mirrors
        # SSA's actual rule: the higher of the two is used.
        spouse_use_spousal = await _should_use_spousal_benefit(
            spouse_row=spouse_row,
            primary_row=primary_row,
            primary_earnings=primary_earnings,
            awi_rows=awi_rows,
            bend_row=bend_row,
            fra_rows=fra_rows,
        )

        if not spouse_use_spousal:
            spouse_earnings_rows = await fetchall(
                "SELECT earn_year, earnings FROM ss_earnings WHERE person_id = %s",
                (spouse_row["id"],),
            )
            spouse_earnings = [
                EarningsRecord(year=r["earn_year"], earnings=r["earnings"])
                for r in spouse_earnings_rows
            ]

    cola_rows_raw = await fetchall(
        "SELECT cola_year AS cola_year, rate FROM ss_cola ORDER BY cola_year"
    )
    cola_rows = [
        {"cola_year": r["cola_year"], "rate": r["rate"]} for r in cola_rows_raw
    ]

    config = OptimizerConfig(
        primary_ss_options=[
            SSClaimingOption(age, 0, _ss_label(age))
            for age in body.primary_ss_claiming_ages
        ],
        spouse_ss_options=[
            SSClaimingOption(age, 0, _ss_label(age))
            for age in body.spouse_ss_claiming_ages
        ]
        if spouse_row
        else [],
        roth_ladder_ceilings=body.roth_ladder_ceilings,
        test_roth_ladder=True,
        test_no_roth_ladder=True,
    )

    result = run_optimizer(
        base_inputs=base_inputs,
        primary_earnings=primary_earnings,
        primary_birth_year=primary_row["birth_year"],
        spouse_birth_year=spouse_birth_year,
        spouse_earnings=spouse_earnings,
        awi_rows=awi_rows,
        bend_point_row=bend_row,
        fra_rules=fra_rows,
        ss_cola_rows=cola_rows,
        config=config,
        spouse_use_spousal_benefit=spouse_use_spousal,
    )

    proj_out = _result_to_out(
        body.scenario_id,
        body.optimize_against_scenario,
        result.projection,
    )

    return {
        "primary_ss_claim_age_years": result.primary_ss_claim_age_years,
        "primary_ss_claim_age_months": result.primary_ss_claim_age_months,
        "primary_ss_claim_label": result.primary_ss_claim_label,
        "spouse_ss_claim_age_years": result.spouse_ss_claim_age_years,
        "spouse_ss_claim_age_months": result.spouse_ss_claim_age_months,
        "spouse_ss_claim_label": result.spouse_ss_claim_label,
        "roth_ladder_enabled": result.roth_ladder_enabled,
        "roth_ladder_target_bracket": result.roth_ladder_target_bracket,
        "portfolio_survives": result.portfolio_survives,
        "residual_balance": result.residual_balance,
        "total_tax_saved_vs_no_ladder": result.total_tax_saved_vs_no_ladder,
        "rationale": result.rationale,
        "projection": proj_out,
    }


# ---------------------------------------------------------------------------
# Spousal benefit auto-detection
# ---------------------------------------------------------------------------


async def _should_use_spousal_benefit(
    spouse_row: dict,
    primary_row: dict,
    primary_earnings: list[EarningsRecord],
    awi_rows: list[AWIRow],
    bend_row: BendPointRow,
    fra_rows: list[FRARule],
) -> bool:
    """
    Return True if the spouse's FRA spousal benefit (50% of primary PIA)
    exceeds their own FRA benefit computed from their earnings record.
    Mirrors SSA's actual determination rule.
    """
    # Compute primary's FRA PIA using primary's own bend points (already loaded)
    primary_fra_est = estimate_benefit(
        birth_year=primary_row["birth_year"],
        claim_age_years=67,
        claim_age_months=0,
        earnings_records=primary_earnings,
        awi_rows=awi_rows,
        bend_point_row=bend_row,
        fra_rules=fra_rows,
        assumed_future_income=0.0,
        current_age=0,
        retirement_age=67,
    )
    spousal_fra_annual = primary_fra_est.pia * 0.50 * 12

    # Compute spouse's own-record FRA benefit using spouse's bend points
    spouse_earnings_rows = await fetchall(
        "SELECT earn_year, earnings FROM ss_earnings WHERE person_id = %s",
        (spouse_row["id"],),
    )
    spouse_earnings = [
        EarningsRecord(year=r["earn_year"], earnings=r["earnings"])
        for r in spouse_earnings_rows
    ]

    spouse_benefit_year = spouse_row["birth_year"] + 62
    spouse_bend_raw = await fetchone(
        "SELECT * FROM ss_bend_points WHERE benefit_year <= %s "
        "ORDER BY benefit_year DESC LIMIT 1",
        (spouse_benefit_year,),
    )
    if not spouse_bend_raw:
        # No bend points for spouse — fall back to spousal
        return True

    spouse_bend = BendPointRow(
        **{
            k: spouse_bend_raw[k]
            for k in [
                "benefit_year",
                "bend_point_1",
                "bend_point_2",
                "factor_below_1",
                "factor_1_to_2",
                "factor_above_2",
            ]
        }
    )

    # FIX: alias both current_income columns to avoid ambiguity now that
    # persons.current_income and scenario_assumptions.current_income both exist.
    spouse_assumptions = await fetchone(
        "SELECT sa.current_income AS household_income, p.current_income AS person_income "
        "FROM scenario_assumptions sa "
        "JOIN persons p ON p.scenario_id = sa.scenario_id "
        "WHERE p.id = %s",
        (spouse_row["id"],),
    )
    spouse_own_est = estimate_benefit(
        birth_year=spouse_row["birth_year"],
        claim_age_years=67,
        claim_age_months=0,
        earnings_records=spouse_earnings,
        awi_rows=awi_rows,
        bend_point_row=spouse_bend,
        fra_rules=fra_rows,
        assumed_future_income=spouse_assumptions["person_income"]
        if spouse_assumptions
        else 0.0,
        current_age=0,
        retirement_age=67,
    )

    return spousal_fra_annual > spouse_own_est.annual_benefit


# ---------------------------------------------------------------------------
# SS reference data loader
# ---------------------------------------------------------------------------


async def _load_ss_reference(person_id: int, birth_year: int):
    earnings_rows = await fetchall(
        "SELECT earn_year, earnings FROM ss_earnings WHERE person_id = %s", (person_id,)
    )
    earnings = [
        EarningsRecord(year=r["earn_year"], earnings=r["earnings"])
        for r in earnings_rows
    ]

    benefit_year = birth_year + 62
    bend_row_raw = await fetchone(
        "SELECT * FROM ss_bend_points WHERE benefit_year <= %s ORDER BY benefit_year DESC LIMIT 1",
        (benefit_year,),
    )
    if not bend_row_raw:
        raise HTTPException(status_code=500, detail="No SS bend point data found")

    awi_rows_raw = await fetchall(
        "SELECT awi_year, awi_value FROM ss_awi ORDER BY awi_year"
    )
    fra_rows_raw = await fetchall("SELECT * FROM ss_fra")

    bend_row = BendPointRow(
        **{
            k: bend_row_raw[k]
            for k in [
                "benefit_year",
                "bend_point_1",
                "bend_point_2",
                "factor_below_1",
                "factor_1_to_2",
                "factor_above_2",
            ]
        }
    )
    awi_rows = [
        AWIRow(year=r["awi_year"], awi_value=r["awi_value"]) for r in awi_rows_raw
    ]
    fra_rules = [
        FRARule(
            **{
                k: r[k]
                for k in ["birth_year_min", "birth_year_max", "fra_years", "fra_months"]
            }
        )
        for r in fra_rows_raw
    ]

    return earnings, bend_row, awi_rows, fra_rules


def _ss_label(age: int) -> str:
    labels = {62: "Early (62)", 67: "FRA (67)", 70: "Late (70)"}
    return labels.get(age, f"Age {age}")