# =============================================================================
# NestEgg - routers/tax.py
# Tax bracket data endpoints and Roth conversion tax modeling.
# =============================================================================

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..database import fetchall, fetchone
from ..utils import current_year
from ..engine.tax_engine import (
    BracketRow, TaxYear, LTCGThresholds,
    compute_ordinary_tax, compute_total_tax,
    bracket_fill_at, roth_conversion_tax_cost,
)

router = APIRouter(prefix="/tax", tags=["tax"])


class TaxEstimateRequest(BaseModel):
    ordinary_income: float
    ltcg_income: float = 0.0
    ss_benefits: float = 0.0
    tax_year: int | None = None
    filing_status: str = "married_filing_jointly"


class RothConversionEstimateRequest(BaseModel):
    existing_income: float
    conversion_amount: float
    tax_year: int | None = None
    filing_status: str = "married_filing_jointly"


@router.get("/brackets")
async def get_tax_brackets(
    tax_year: int | None = None,
    filing_status: str = "married_filing_jointly",
):
    """Return tax brackets and standard deduction for a given year."""
    yr = tax_year or current_year()
    brackets = await fetchall(
        "SELECT rate, income_min, income_max FROM tax_brackets "
        "WHERE tax_year = %s AND filing_status = %s ORDER BY income_min",
        (yr, filing_status),
    )
    std_deduction = await fetchone(
        "SELECT amount FROM standard_deductions WHERE tax_year = %s AND filing_status = %s",
        (yr, filing_status),
    )
    return {
        "tax_year": yr,
        "filing_status": filing_status,
        "standard_deduction": std_deduction["amount"] if std_deduction else None,
        "brackets": brackets,
    }


@router.post("/estimate")
async def estimate_tax(body: TaxEstimateRequest):
    """Compute federal tax for a given income mix."""
    tax_year_obj = await _load_tax_year(body.tax_year, body.filing_status)
    result = compute_total_tax(
        ordinary_income=body.ordinary_income,
        ltcg_income=body.ltcg_income,
        ss_benefits=body.ss_benefits,
        tax_year=tax_year_obj,
        ltcg_thresholds=LTCGThresholds(),
        include_bracket_detail=True,
    )
    return {
        "ordinary_taxable_income": result.ordinary.taxable_income,
        "standard_deduction": result.ordinary.standard_deduction,
        "ordinary_tax": result.ordinary.tax_owed,
        "effective_rate": result.ordinary.effective_rate,
        "marginal_rate": result.ordinary.marginal_rate,
        "ltcg_tax": result.ltcg.ltcg_tax,
        "niit": result.ltcg.niit,
        "ss_taxable_amount": result.ss_taxable_amount,
        "total_tax": result.total_tax,
        "total_effective_rate": result.total_effective_rate,
        "bracket_detail": result.ordinary.bracket_detail,
    }


@router.post("/roth-conversion-cost")
async def roth_conversion_cost(body: RothConversionEstimateRequest):
    """Compute the incremental tax cost of a Roth conversion."""
    tax_year_obj = await _load_tax_year(body.tax_year, body.filing_status)
    cost = roth_conversion_tax_cost(
        conversion_amount=body.conversion_amount,
        existing_income=body.existing_income,
        tax_year=tax_year_obj,
    )
    fill = bracket_fill_at(body.existing_income, tax_year_obj)
    return {
        "conversion_amount": body.conversion_amount,
        "existing_income": body.existing_income,
        "incremental_tax_cost": cost,
        "effective_conversion_rate": cost / body.conversion_amount if body.conversion_amount else 0,
        "current_marginal_rate": fill.current_rate,
        "bracket_room_remaining": fill.current_bracket_remaining,
        "next_bracket_rate": fill.next_rate,
    }


async def _load_tax_year(year: int | None, filing_status: str) -> TaxYear:
    yr = year or current_year()
    brackets_raw = await fetchall(
        "SELECT rate, income_min, income_max FROM tax_brackets "
        "WHERE tax_year = %s AND filing_status = %s ORDER BY income_min",
        (yr, filing_status),
    )
    if not brackets_raw:
        # Fallback to most recent year
        latest = await fetchone(
            "SELECT MAX(tax_year) AS yr FROM tax_brackets WHERE filing_status = %s",
            (filing_status,),
        )
        yr = latest["yr"]
        brackets_raw = await fetchall(
            "SELECT rate, income_min, income_max FROM tax_brackets "
            "WHERE tax_year = %s AND filing_status = %s ORDER BY income_min",
            (yr, filing_status),
        )

    std = await fetchone(
        "SELECT amount FROM standard_deductions WHERE tax_year = %s AND filing_status = %s",
        (yr, filing_status),
    )

    return TaxYear(
        year=yr,
        brackets=[BracketRow(
            rate=r["rate"],
            income_min=r["income_min"],
            income_max=r["income_max"],
        ) for r in brackets_raw],
        standard_deduction=std["amount"] if std else 30000.0,
        filing_status=filing_status,
    )
