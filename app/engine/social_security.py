# =============================================================================
# NestEgg - engine/social_security.py
# Social Security benefit estimation using SSA actuarial formulas.
#
# Covers:
#   - Earnings indexing to the year the worker turns 60 (AWI-based)
#   - AIME calculation (top 35 years of indexed earnings)
#   - PIA calculation via bend point formula
#   - Early/late claiming adjustments relative to FRA
#   - Spousal benefit (50% of primary's PIA, subject to reductions)
#   - Benefit commencement by calendar year (for projection engine)
#
# All reference data (bend points, AWI, FRA, COLA) passed in from DB.
# =============================================================================

from __future__ import annotations

from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Data containers (populated from DB rows)
# ---------------------------------------------------------------------------


@dataclass
class EarningsRecord:
    """One year of actual earnings for a person."""

    year: int
    earnings: float


@dataclass
class BendPointRow:
    """One row from ss_bend_points table."""

    benefit_year: int  # Year person turns 62
    bend_point_1: float
    bend_point_2: float
    factor_below_1: float  # Typically 0.90
    factor_1_to_2: float  # Typically 0.32
    factor_above_2: float  # Typically 0.15


@dataclass
class AWIRow:
    """One row from ss_awi table."""

    year: int
    awi_value: float


@dataclass
class FRARule:
    """Full Retirement Age rule for a birth year range."""

    birth_year_min: int
    birth_year_max: int
    fra_years: int
    fra_months: int


# ---------------------------------------------------------------------------
# Result containers
# ---------------------------------------------------------------------------


@dataclass
class SSBenefitEstimate:
    """
    Full benefit estimate for one person at a given claiming age.
    """

    person_birth_year: int
    claim_age_years: int
    claim_age_months: int
    fra_years: int
    fra_months: int
    aime: float  # Average Indexed Monthly Earnings
    pia: float  # Primary Insurance Amount (at FRA)
    monthly_benefit: float  # After early/late adjustment
    annual_benefit: float  # monthly_benefit * 12
    adjustment_factor: float  # Multiplier applied to PIA (< 1 early, > 1 late)
    is_early: bool
    is_late: bool
    months_from_fra: int  # Negative = early, Positive = late


@dataclass
class SSClaimingComparison:
    """Side-by-side comparison of early / FRA / late claiming."""

    early: SSBenefitEstimate  # Age 62
    fra: SSBenefitEstimate  # Full retirement age
    late: SSBenefitEstimate  # Age 70


@dataclass
class SpousalBenefit:
    """Spousal benefit calculation result."""

    primary_pia: float
    spousal_raw: float  # 50% of primary PIA
    spousal_reduction: float  # Reduction if claiming before spouse's FRA
    spousal_monthly: float  # Final monthly benefit
    spousal_annual: float


# ---------------------------------------------------------------------------
# Helper: Full Retirement Age lookup
# ---------------------------------------------------------------------------


def get_fra(birth_year: int, fra_rules: list[FRARule]) -> tuple[int, int]:
    """
    Return (fra_years, fra_months) for a given birth year.
    """
    for rule in fra_rules:
        if rule.birth_year_min <= birth_year <= rule.birth_year_max:
            return rule.fra_years, rule.fra_months
    # Default for 1960+
    return 67, 0


def fra_in_months(birth_year: int, fra_rules: list[FRARule]) -> int:
    """Return FRA expressed as total months from birth."""
    y, m = get_fra(birth_year, fra_rules)
    return y * 12 + m


# ---------------------------------------------------------------------------
# Earnings indexing
# ---------------------------------------------------------------------------


def index_earnings(
    earnings_records: list[EarningsRecord],
    birth_year: int,
    awi_rows: list[AWIRow],
) -> list[float]:
    """
    Index each year's earnings to the year the worker turns 60.
    Earnings after age 60 are used at face value (index factor = 1.0).

    The indexing year = birth_year + 60.
    Index factor for year Y = AWI(index_year) / AWI(Y).

    Returns:
        List of indexed earnings, one per record (same order as input).
    """
    index_year = birth_year + 60
    awi_map = {row.year: row.awi_value for row in awi_rows}

    # Use the most recent available AWI as a proxy for missing future years
    max_awi_year = max(awi_map.keys())
    index_awi = awi_map.get(index_year) or awi_map[max_awi_year]

    indexed: list[float] = []
    for record in earnings_records:
        earn_awi = awi_map.get(record.year) or awi_map[max_awi_year]
        if record.year >= index_year:
            # After age 60: use nominal earnings (index = 1.0)
            indexed.append(record.earnings)
        else:
            factor = index_awi / earn_awi
            indexed.append(record.earnings * factor)

    return indexed


# ---------------------------------------------------------------------------
# AIME calculation
# ---------------------------------------------------------------------------


def compute_aime(
    earnings_records: list[EarningsRecord],
    birth_year: int,
    awi_rows: list[AWIRow],
    assumed_future_income: float = 0.0,
    current_age: int = 0,
    retirement_age: int = 0,
) -> float:
    """
    Compute Average Indexed Monthly Earnings (AIME).

    Uses the top 35 years of indexed earnings. If the person has fewer than
    35 years, zeros fill the remainder.

    If `assumed_future_income` is provided and the person hasn't retired yet,
    future years (from current_age to retirement_age) are projected at that
    income level and included in the top-35 selection.

    Args:
        earnings_records:      Historical earnings from DB.
        birth_year:            Person's birth year.
        awi_rows:              AWI table from DB.
        assumed_future_income: Current income used to project remaining working years.
        current_age:           Person's current age (for projecting future years).
        retirement_age:        Planned retirement age (stops earning after this).

    Returns:
        AIME (monthly average of top 35 indexed earnings years).
    """
    records = list(earnings_records)

    # Project future years if applicable
    if assumed_future_income > 0 and current_age > 0 and retirement_age > current_age:
        current_year = birth_year + current_age
        retire_year = birth_year + retirement_age
        existing_years = {r.year for r in records}
        for yr in range(current_year, retire_year):
            if yr not in existing_years:
                records.append(EarningsRecord(year=yr, earnings=assumed_future_income))

    indexed = index_earnings(records, birth_year, awi_rows)

    # Top 35 years (pad with zeros if < 35 years of earnings)
    top_35 = sorted(indexed, reverse=True)[:35]
    while len(top_35) < 35:
        top_35.append(0.0)

    total_indexed = sum(top_35)
    # AIME = total / (35 years * 12 months)
    return total_indexed / (35 * 12)


# ---------------------------------------------------------------------------
# PIA calculation
# ---------------------------------------------------------------------------


def compute_pia(aime: float, bend_point_row: BendPointRow) -> float:
    """
    Compute Primary Insurance Amount (PIA) using the bend point formula.

    PIA = (factor_below_1 * min(AIME, BP1))
        + (factor_1_to_2  * max(0, min(AIME, BP2) - BP1))
        + (factor_above_2 * max(0, AIME - BP2))

    Args:
        aime:           Computed AIME.
        bend_point_row: Bend points for the year the person turns 62.

    Returns:
        PIA in monthly dollars (rounded to nearest $0.10 per SSA rules).
    """
    bp1 = bend_point_row.bend_point_1
    bp2 = bend_point_row.bend_point_2

    segment1 = bend_point_row.factor_below_1 * min(aime, bp1)
    segment2 = bend_point_row.factor_1_to_2 * max(0.0, min(aime, bp2) - bp1)
    segment3 = bend_point_row.factor_above_2 * max(0.0, aime - bp2)

    pia_raw = segment1 + segment2 + segment3
    # SSA rounds down to nearest $0.10
    return round(pia_raw * 10) / 10


# ---------------------------------------------------------------------------
# Claiming age adjustments
# ---------------------------------------------------------------------------


def claiming_adjustment_factor(
    claim_age_years: int,
    claim_age_months: int,
    fra_years: int,
    fra_months: int,
) -> tuple[float, int]:
    """
    Calculate the benefit adjustment factor for claiming before or after FRA.

    Early claiming (before FRA):
        - 5/9 of 1% per month for the first 36 months before FRA
        - 5/12 of 1% per month beyond 36 months before FRA

    Late claiming (after FRA, up to age 70):
        - 8% per year (2/3 of 1% per month) Delayed Retirement Credits

    Args:
        claim_age_years/months:   Intended claiming age.
        fra_years/fra_months:     Full retirement age for this person.

    Returns:
        (adjustment_factor, months_from_fra)
        months_from_fra is negative for early, positive for late.
    """
    claim_total_months = claim_age_years * 12 + claim_age_months
    fra_total_months = fra_years * 12 + fra_months
    months_diff = claim_total_months - fra_total_months

    if months_diff == 0:
        return 1.0, 0

    if months_diff > 0:
        # Late claiming: +2/3% per month (8% per year), max age 70
        max_late_months = (70 * 12) - fra_total_months
        effective_months = min(months_diff, max_late_months)
        factor = 1.0 + (effective_months * (2 / 3) / 100)
        return factor, months_diff

    # Early claiming
    early_months = abs(months_diff)
    if early_months <= 36:
        reduction = early_months * (5 / 9) / 100
    else:
        reduction = 36 * (5 / 9) / 100 + (early_months - 36) * (5 / 12) / 100

    factor = 1.0 - reduction
    return factor, months_diff


# ---------------------------------------------------------------------------
# Full benefit estimate
# ---------------------------------------------------------------------------


def estimate_benefit(
    birth_year: int,
    claim_age_years: int,
    claim_age_months: int,
    earnings_records: list[EarningsRecord],
    awi_rows: list[AWIRow],
    bend_point_row: BendPointRow,
    fra_rules: list[FRARule],
    assumed_future_income: float = 0.0,
    current_age: int = 0,
    retirement_age: int = 0,
) -> SSBenefitEstimate:
    """
    Full benefit estimate for a single person at a specific claiming age.
    """
    fra_years, fra_months = get_fra(birth_year, fra_rules)

    aime = compute_aime(
        earnings_records=earnings_records,
        birth_year=birth_year,
        awi_rows=awi_rows,
        assumed_future_income=assumed_future_income,
        current_age=current_age,
        retirement_age=retirement_age,
    )

    pia = compute_pia(aime, bend_point_row)

    factor, months_from_fra = claiming_adjustment_factor(
        claim_age_years, claim_age_months, fra_years, fra_months
    )

    monthly = round(pia * factor, 2)
    annual = monthly * 12

    return SSBenefitEstimate(
        person_birth_year=birth_year,
        claim_age_years=claim_age_years,
        claim_age_months=claim_age_months,
        fra_years=fra_years,
        fra_months=fra_months,
        aime=aime,
        pia=pia,
        monthly_benefit=monthly,
        annual_benefit=annual,
        adjustment_factor=factor,
        is_early=months_from_fra < 0,
        is_late=months_from_fra > 0,
        months_from_fra=months_from_fra,
    )


def build_claiming_comparison(
    birth_year: int,
    earnings_records: list[EarningsRecord],
    awi_rows: list[AWIRow],
    bend_point_row: BendPointRow,
    fra_rules: list[FRARule],
    assumed_future_income: float = 0.0,
    current_age: int = 0,
    retirement_age: int = 0,
) -> SSClaimingComparison:
    """Build early (62) / FRA / late (70) comparison for the UI."""
    fra_years, fra_months = get_fra(birth_year, fra_rules)

    def est(y: int, m: int) -> SSBenefitEstimate:
        return estimate_benefit(
            birth_year=birth_year,
            claim_age_years=y,
            claim_age_months=m,
            earnings_records=earnings_records,
            awi_rows=awi_rows,
            bend_point_row=bend_point_row,
            fra_rules=fra_rules,
            assumed_future_income=assumed_future_income,
            current_age=current_age,
            retirement_age=retirement_age,
        )

    return SSClaimingComparison(
        early=est(62, 0),
        fra=est(fra_years, fra_months),
        late=est(70, 0),
    )


# ---------------------------------------------------------------------------
# Spousal benefit
# ---------------------------------------------------------------------------


def compute_spousal_benefit(
    primary_pia: float,
    spouse_claim_age_years: int,
    spouse_claim_age_months: int,
    spouse_birth_year: int,
    fra_rules: list[FRARule],
    spousal_pct: float = 0.50,
) -> SpousalBenefit:
    """
    Compute the spousal benefit for a non-working or lower-earning spouse.

    The spousal benefit is 50% of the primary worker's PIA at the spouse's FRA.
    If the spouse claims before their own FRA, the spousal benefit is reduced.
    Spousal benefit cannot be increased by delayed claiming past FRA.

    Args:
        primary_pia:              PIA of the primary earner.
        spouse_claim_age_*:       Age at which the spouse claims.
        spouse_birth_year:        Spouse's birth year (for FRA lookup).
        fra_rules:                FRA table from DB.
        spousal_pct:              Fraction of primary PIA (default 0.50).
    """
    spouse_fra_years, spouse_fra_months = get_fra(spouse_birth_year, fra_rules)
    raw_spousal = primary_pia * spousal_pct

    # Check if spouse is claiming before their FRA
    claim_months = spouse_claim_age_years * 12 + spouse_claim_age_months
    fra_months_total = spouse_fra_years * 12 + spouse_fra_months
    months_early = fra_months_total - claim_months

    reduction = 0.0
    if months_early > 0:
        # Reduction: 25/36 of 1% per month for first 36 months,
        # 5/12 of 1% per month beyond 36 months
        if months_early <= 36:
            reduction = months_early * (25 / 36) / 100
        else:
            reduction = 36 * (25 / 36) / 100 + (months_early - 36) * (5 / 12) / 100

    final_monthly = round(raw_spousal * (1 - reduction), 2)

    return SpousalBenefit(
        primary_pia=primary_pia,
        spousal_raw=raw_spousal,
        spousal_reduction=reduction,
        spousal_monthly=final_monthly,
        spousal_annual=final_monthly * 12,
    )


# ---------------------------------------------------------------------------
# Projection helper: benefit amount for a given calendar year
# ---------------------------------------------------------------------------


def annual_benefit_in_year(
    benefit_estimate: SSBenefitEstimate,
    claim_start_year: int,  # Calendar year benefits begin
    projection_year: int,  # Calendar year being projected
    cola_rows: list[dict],  # [{"cola_year": int, "rate": float}]
    assumed_cola: float = 0.025,  # Assumed future COLA rate
) -> float:
    """
    Return the annual SS benefit in a given projection year, including COLA.

    Before claim_start_year: returns 0.
    In claim_start_year and beyond: applies cumulative COLA adjustments.

    Args:
        benefit_estimate:  Computed benefit (monthly/annual at claim start).
        claim_start_year:  Year benefits begin.
        projection_year:   Year for which we want the benefit amount.
        cola_rows:         Historical COLA data from DB.
        assumed_cola:      COLA assumption for future years not yet in DB.
    """
    if projection_year < claim_start_year:
        return 0.0

    cola_map = {row["cola_year"]: row["rate"] for row in cola_rows}
    benefit = benefit_estimate.annual_benefit

    for yr in range(claim_start_year + 1, projection_year + 1):
        cola = cola_map.get(yr, assumed_cola)
        benefit *= 1 + cola

    return round(benefit, 2)
