# =============================================================================
# NestEgg - routers/social_security.py
# Social Security earnings upload, benefit estimation, and claiming comparison.
# =============================================================================

from __future__ import annotations

from fastapi import APIRouter, HTTPException, UploadFile, File

from ..database import execute, executemany, fetchall, fetchone
from ..models import (
    SSClaimingCreate,
    SSClaimingComparisonOut,
    SSClaimingOut,
    SSBenefitEstimateOut,
    MessageResponse,
)
from ..utils import parse_ss_earnings_csv, current_year
from ..engine.social_security import (
    EarningsRecord,
    BendPointRow,
    AWIRow,
    FRARule,
    estimate_benefit,
    build_claiming_comparison,
    compute_spousal_benefit,
    get_fra,
)

router = APIRouter(prefix="/social-security", tags=["social_security"])


# ---------------------------------------------------------------------------
# Upload SS earnings CSV
# ---------------------------------------------------------------------------


@router.post("/earnings/{person_id}/upload", response_model=MessageResponse)
async def upload_earnings(person_id: int, file: UploadFile = File(...)):
    person = await fetchone("SELECT id FROM persons WHERE id = %s", (person_id,))
    if not person:
        raise HTTPException(status_code=404, detail="Person not found")

    content = await file.read()
    try:
        rows = parse_ss_earnings_csv(content)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    # Delete existing earnings for this person and re-insert
    await execute("DELETE FROM ss_earnings WHERE person_id = %s", (person_id,))
    await executemany(
        "INSERT INTO ss_earnings (person_id, earn_year, earnings) VALUES (%s, %s, %s)",
        [(person_id, r["year"], r["earnings"]) for r in rows],
    )

    return {"message": f"Uploaded {len(rows)} earnings records for person {person_id}."}


# ---------------------------------------------------------------------------
# Get earnings history for a person
# ---------------------------------------------------------------------------


@router.get("/earnings/{person_id}", response_model=list[dict])
async def get_earnings(person_id: int):
    person = await fetchone("SELECT id FROM persons WHERE id = %s", (person_id,))
    if not person:
        raise HTTPException(status_code=404, detail="Person not found")

    rows = await fetchall(
        "SELECT earn_year AS year, earnings FROM ss_earnings "
        "WHERE person_id = %s ORDER BY earn_year",
        (person_id,),
    )
    return rows


# ---------------------------------------------------------------------------
# Save / update SS claiming strategy for a person
# ---------------------------------------------------------------------------


@router.put("/claiming/{person_id}", response_model=SSClaimingOut)
async def upsert_claiming(person_id: int, body: SSClaimingCreate):
    person = await fetchone("SELECT id FROM persons WHERE id = %s", (person_id,))
    if not person:
        raise HTTPException(status_code=404, detail="Person not found")

    existing = await fetchone(
        "SELECT id FROM ss_claiming WHERE person_id = %s", (person_id,)
    )
    if existing:
        await execute(
            """UPDATE ss_claiming SET
               claim_age_years = %s, claim_age_months = %s,
               use_spousal_benefit = %s, spousal_benefit_pct = %s
               WHERE person_id = %s""",
            (
                body.claim_age_years,
                body.claim_age_months,
                body.use_spousal_benefit,
                body.spousal_benefit_pct,
                person_id,
            ),
        )
    else:
        await execute(
            """INSERT INTO ss_claiming
               (person_id, claim_age_years, claim_age_months,
                use_spousal_benefit, spousal_benefit_pct)
               VALUES (%s, %s, %s, %s, %s)""",
            (
                person_id,
                body.claim_age_years,
                body.claim_age_months,
                body.use_spousal_benefit,
                body.spousal_benefit_pct,
            ),
        )

    row = await fetchone("SELECT * FROM ss_claiming WHERE person_id = %s", (person_id,))
    return row


# ---------------------------------------------------------------------------
# Benefit estimate for a specific claiming age
# ---------------------------------------------------------------------------


@router.get("/estimate/{person_id}", response_model=SSBenefitEstimateOut)
async def get_benefit_estimate(
    person_id: int,
    claim_age_years: int = 67,
    claim_age_months: int = 0,
):
    (
        person,
        earnings_rows,
        bend_row,
        awi_rows,
        fra_rows,
        assumptions,
    ) = await _load_ss_data(person_id)

    earnings = [
        EarningsRecord(year=r["earn_year"], earnings=r["earnings"])
        for r in earnings_rows
    ]

    result = estimate_benefit(
        birth_year=person["birth_year"],
        claim_age_years=claim_age_years,
        claim_age_months=claim_age_months,
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
        assumed_future_income=assumptions["current_income"] if assumptions else 0.0,
        current_age=current_year() - person["birth_year"],
        retirement_age=person["planned_retirement_age"],
    )

    return _benefit_to_out(result)


# ---------------------------------------------------------------------------
# Early / FRA / late comparison
# ---------------------------------------------------------------------------


@router.get("/comparison/{person_id}", response_model=SSClaimingComparisonOut)
async def get_claiming_comparison(person_id: int):
    (
        person,
        earnings_rows,
        bend_row,
        awi_rows,
        fra_rows,
        assumptions,
    ) = await _load_ss_data(person_id)

    earnings = [
        EarningsRecord(year=r["earn_year"], earnings=r["earnings"])
        for r in earnings_rows
    ]

    awi_list = [AWIRow(year=r["awi_year"], awi_value=r["awi_value"]) for r in awi_rows]
    bend = BendPointRow(
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
    )
    fra_rules = [
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
    ]

    # Compute own-record comparison
    own_comparison = build_claiming_comparison(
        birth_year=person["birth_year"],
        earnings_records=earnings,
        awi_rows=awi_list,
        bend_point_row=bend,
        fra_rules=fra_rules,
        assumed_future_income=assumptions["current_income"] if assumptions else 0.0,
        current_age=current_year() - person["birth_year"],
        retirement_age=person["planned_retirement_age"],
    )
    own_fra_annual = own_comparison.fra.annual_benefit

    # Check whether this person has a primary spouse whose PIA we can use for
    # a spousal benefit comparison. Look for the other person in the same scenario.
    primary_row = await fetchone(
        """SELECT p2.id, p2.birth_year FROM persons p2
           JOIN persons p1 ON p1.scenario_id = p2.scenario_id
           WHERE p1.id = %s AND p2.role = 'primary' AND p2.id != %s""",
        (person_id, person_id),
    )

    benefit_basis = "own_record"
    comparison = own_comparison

    if primary_row:
        # Compute primary's FRA PIA so we can derive the spousal benefit
        primary_earnings_rows = await fetchall(
            "SELECT earn_year, earnings FROM ss_earnings WHERE person_id = %s",
            (primary_row["id"],),
        )
        primary_earnings = [
            EarningsRecord(year=r["earn_year"], earnings=r["earnings"])
            for r in primary_earnings_rows
        ]
        primary_assumptions = await fetchone(
            "SELECT current_income FROM scenario_assumptions sa "
            "JOIN persons p ON p.scenario_id = sa.scenario_id "
            "WHERE p.id = %s",
            (primary_row["id"],),
        )

        # Get primary's bend points (keyed to primary's birth year)
        primary_benefit_year = primary_row["birth_year"] + 62
        primary_bend_raw = await fetchone(
            "SELECT * FROM ss_bend_points WHERE benefit_year <= %s "
            "ORDER BY benefit_year DESC LIMIT 1",
            (primary_benefit_year,),
        )
        if primary_bend_raw:
            primary_bend = BendPointRow(
                **{
                    k: primary_bend_raw[k]
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
            primary_fra_est = estimate_benefit(
                birth_year=primary_row["birth_year"],
                claim_age_years=67,
                claim_age_months=0,
                earnings_records=primary_earnings,
                awi_rows=awi_list,
                bend_point_row=primary_bend,
                fra_rules=fra_rules,
                assumed_future_income=primary_assumptions["current_income"]
                if primary_assumptions
                else 0.0,
                current_age=current_year() - primary_row["birth_year"],
                retirement_age=67,
            )
            primary_pia = primary_fra_est.pia

            # Spousal benefit at FRA = 50% of primary PIA.
            # If this exceeds the spouse's own FRA benefit, use spousal for all
            # three claiming ages — this mirrors SSA's actual determination.
            spousal_fra_annual = primary_pia * 0.50 * 12
            if spousal_fra_annual > own_fra_annual:
                benefit_basis = "spousal"
                # Build spousal comparison across early/FRA/late claiming ages.
                # Spousal benefit is reduced for early claiming and does NOT
                # earn delayed retirement credits past FRA.
                spouse_fra_y, spouse_fra_m = get_fra(person["birth_year"], fra_rules)
                spousal_estimates = []
                for claim_years, claim_months, label in [
                    (62, 0, "Early (62)"),
                    (spouse_fra_y, spouse_fra_m, "FRA"),
                    (70, 0, "Late (70)"),
                ]:
                    spousal = compute_spousal_benefit(
                        primary_pia=primary_pia,
                        spouse_claim_age_years=claim_years,
                        spouse_claim_age_months=claim_months,
                        spouse_birth_year=person["birth_year"],
                        fra_rules=fra_rules,
                        spousal_pct=0.50,
                    )
                    from ..engine.social_security import claiming_adjustment_factor
                    adj, months_from_fra = claiming_adjustment_factor(
                        claim_years, claim_months, spouse_fra_y, spouse_fra_m
                    )
                    spousal_estimates.append(
                        SSBenefitEstimateOut(
                            claim_age_years=claim_years,
                            claim_age_months=claim_months,
                            fra_years=spouse_fra_y,
                            fra_months=spouse_fra_m,
                            aime=0.0,  # not applicable for spousal
                            pia=primary_pia * 0.50,
                            monthly_benefit=spousal.spousal_monthly,
                            annual_benefit=spousal.spousal_annual,
                            adjustment_factor=adj,
                            is_early=months_from_fra < 0,
                            is_late=months_from_fra > 0,
                            months_from_fra=months_from_fra,
                        )
                    )

                return SSClaimingComparisonOut(
                    early=spousal_estimates[0],
                    fra=spousal_estimates[1],
                    late=spousal_estimates[2],
                    benefit_basis="spousal",
                )

    return SSClaimingComparisonOut(
        early=_benefit_to_out(comparison.early),
        fra=_benefit_to_out(comparison.fra),
        late=_benefit_to_out(comparison.late),
        benefit_basis=benefit_basis,
    )


# ---------------------------------------------------------------------------
# Shared data loader
# ---------------------------------------------------------------------------


async def _load_ss_data(person_id: int):
    person = await fetchone("SELECT * FROM persons WHERE id = %s", (person_id,))
    if not person:
        raise HTTPException(status_code=404, detail="Person not found")

    earnings_rows = await fetchall(
        "SELECT earn_year, earnings FROM ss_earnings WHERE person_id = %s ORDER BY earn_year",
        (person_id,),
    )

    benefit_year = person["birth_year"] + 62
    bend_row = await fetchone(
        "SELECT * FROM ss_bend_points WHERE benefit_year <= %s ORDER BY benefit_year DESC LIMIT 1",
        (benefit_year,),
    )
    if not bend_row:
        raise HTTPException(status_code=500, detail="No SS bend point data available")

    awi_rows = await fetchall(
        "SELECT awi_year, awi_value FROM ss_awi ORDER BY awi_year"
    )
    fra_rows = await fetchall("SELECT * FROM ss_fra")

    assumptions = await fetchone(
        "SELECT current_income FROM scenario_assumptions sa "
        "JOIN persons p ON p.scenario_id = sa.scenario_id "
        "WHERE p.id = %s",
        (person_id,),
    )

    return person, earnings_rows, bend_row, awi_rows, fra_rows, assumptions


def _benefit_to_out(b) -> SSBenefitEstimateOut:
    return SSBenefitEstimateOut(
        claim_age_years=b.claim_age_years,
        claim_age_months=b.claim_age_months,
        fra_years=b.fra_years,
        fra_months=b.fra_months,
        aime=b.aime,
        pia=b.pia,
        monthly_benefit=b.monthly_benefit,
        annual_benefit=b.annual_benefit,
        adjustment_factor=b.adjustment_factor,
        is_early=b.is_early,
        is_late=b.is_late,
        months_from_fra=b.months_from_fra,
    )
