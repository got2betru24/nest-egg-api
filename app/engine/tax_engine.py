# =============================================================================
# NestEgg - engine/tax_engine.py
# Federal income tax calculations for retirement planning.
#
# Covers:
#   - Ordinary income tax (brackets passed in from DB)
#   - Standard deduction
#   - Long-term capital gains (LTCG) tax on brokerage withdrawals
#   - Net Investment Income Tax (NIIT) 3.8% above threshold
#   - Social Security taxability (up to 85% includable)
#   - Roth conversion marginal cost modeling
#   - Effective rate and bracket fill calculations (for optimizer)
#
# All bracket/deduction data is passed in — no DB access.
# All calculations assume Married Filing Jointly (MFJ).
# =============================================================================

from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Data containers (passed in from DB)
# ---------------------------------------------------------------------------


@dataclass
class BracketRow:
    """One row from the tax_brackets table."""

    rate: float
    income_min: float
    income_max: float | None  # None = top bracket (no cap)


@dataclass
class TaxYear:
    """All tax parameters for a given year, passed in from DB."""

    year: int
    brackets: list[BracketRow]  # Must be sorted ascending by income_min
    standard_deduction: float
    filing_status: str = "married_filing_jointly"


# ---------------------------------------------------------------------------
# LTCG / NIIT thresholds (MFJ 2025 — inflated in caller if needed)
# ---------------------------------------------------------------------------


@dataclass
class LTCGThresholds:
    """
    Long-term capital gains rate thresholds (MFJ).
    Caller should inflate these if projecting into future years.
    """

    zero_max: float = 96_700.00  # 0% LTCG up to this taxable income
    fifteen_max: float = 600_050.00  # 15% LTCG up to this taxable income
    niit_threshold: float = 250_000.00  # 3.8% NIIT on investment income above this AGI


# ---------------------------------------------------------------------------
# Result containers
# ---------------------------------------------------------------------------


@dataclass
class OrdinaryTaxResult:
    """Result of compute_ordinary_tax()."""

    gross_income: float
    standard_deduction: float
    taxable_income: float
    tax_owed: float
    effective_rate: float
    marginal_rate: float
    bracket_detail: list[dict] = field(default_factory=list)


@dataclass
class LTCGTaxResult:
    """Result of compute_ltcg_tax()."""

    gains_amount: float
    ordinary_taxable_income: float  # AGI before gains
    rate_applied: float
    ltcg_tax: float
    niit: float
    total_tax: float


@dataclass
class TotalTaxResult:
    """Combined ordinary + LTCG tax for a retirement year."""

    ordinary: OrdinaryTaxResult
    ltcg: LTCGTaxResult
    total_tax: float
    total_effective_rate: float  # total_tax / gross_income (incl. gains)
    ss_taxable_amount: float  # How much of SS benefits are taxable


@dataclass
class BracketFill:
    """How much room remains in the current and next brackets (for optimizer)."""

    current_rate: float
    current_bracket_remaining: float  # Room left before hitting next bracket
    next_rate: float | None
    next_bracket_size: float | None  # Full width of the next bracket


# ---------------------------------------------------------------------------
# Social Security taxability
# ---------------------------------------------------------------------------


def ss_taxable_amount(
    ss_gross: float,
    other_income: float,
    filing_status: str = "married_filing_jointly",
) -> float:
    """
    Calculate the taxable portion of Social Security benefits.
    Uses the provisional income (combined income) test.

    MFJ thresholds:
        < $32,000  → 0% taxable
        $32,000–$44,000 → up to 50% taxable
        > $44,000  → up to 85% taxable

    Args:
        ss_gross:     Total SS benefits (both spouses combined).
        other_income: All other income (wages, 401k withdrawals, Roth conversions).

    Returns:
        Taxable portion of SS benefits (never exceeds 0.85 * ss_gross).
    """
    provisional = other_income + 0.5 * ss_gross

    if filing_status == "married_filing_jointly":
        lower_threshold = 32_000.0
        upper_threshold = 44_000.0
    else:  # single
        lower_threshold = 25_000.0
        upper_threshold = 34_000.0

    if provisional <= lower_threshold:
        return 0.0

    if provisional <= upper_threshold:
        # Up to 50% of SS is taxable
        taxable = 0.5 * (provisional - lower_threshold)
    else:
        # Up to 85% of SS is taxable
        taxable = 0.5 * (upper_threshold - lower_threshold) + 0.85 * (
            provisional - upper_threshold
        )

    return min(taxable, 0.85 * ss_gross)


# ---------------------------------------------------------------------------
# Core tax computation
# ---------------------------------------------------------------------------


def compute_ordinary_tax(
    gross_income: float,
    tax_year: TaxYear,
    include_bracket_detail: bool = False,
) -> OrdinaryTaxResult:
    """
    Compute federal ordinary income tax using progressive brackets.

    Args:
        gross_income:           Total ordinary income (wages, 401k withdrawals,
                                SS taxable portion, Roth conversions).
        tax_year:               Brackets and standard deduction for the year.
        include_bracket_detail: If True, populate bracket_detail for UI display.

    Returns:
        OrdinaryTaxResult with tax owed, effective rate, marginal rate.
    """
    taxable = max(0.0, gross_income - tax_year.standard_deduction)

    tax_owed = 0.0
    marginal_rate = 0.0
    detail: list[dict] = []

    brackets = sorted(tax_year.brackets, key=lambda b: b.income_min)

    for bracket in brackets:
        b_min = bracket.income_min
        b_max = bracket.income_max if bracket.income_max is not None else float("inf")

        if taxable <= b_min:
            break

        income_in_bracket = min(taxable, b_max) - b_min
        tax_in_bracket = income_in_bracket * bracket.rate
        tax_owed += tax_in_bracket
        marginal_rate = bracket.rate

        if include_bracket_detail:
            detail.append(
                {
                    "rate": bracket.rate,
                    "income_in_bracket": income_in_bracket,
                    "tax_in_bracket": tax_in_bracket,
                }
            )

    effective_rate = tax_owed / gross_income if gross_income > 0 else 0.0

    return OrdinaryTaxResult(
        gross_income=gross_income,
        standard_deduction=tax_year.standard_deduction,
        taxable_income=taxable,
        tax_owed=tax_owed,
        effective_rate=effective_rate,
        marginal_rate=marginal_rate,
        bracket_detail=detail,
    )


def compute_ltcg_tax(
    gains_amount: float,
    ordinary_taxable_income: float,
    thresholds: LTCGThresholds | None = None,
) -> LTCGTaxResult:
    """
    Compute LTCG tax on brokerage account gains/withdrawals.

    The LTCG rate depends on the taxpayer's total income including the gains.
    Stacking rule: ordinary income fills the bracket first, then gains are
    taxed at the LTCG rate applicable to the income tier they fall in.

    Args:
        gains_amount:             Amount of long-term capital gains.
        ordinary_taxable_income:  Taxable ordinary income (after std deduction)
                                  BEFORE adding gains.
    """
    if thresholds is None:
        thresholds = LTCGThresholds()

    if gains_amount <= 0:
        return LTCGTaxResult(
            gains_amount=0.0,
            ordinary_taxable_income=ordinary_taxable_income,
            rate_applied=0.0,
            ltcg_tax=0.0,
            niit=0.0,
            total_tax=0.0,
        )

    # Stacking: ordinary income is "below" the gains
    income_below_gains = ordinary_taxable_income
    total_income_with_gains = income_below_gains + gains_amount

    ltcg_tax = 0.0
    remaining_gains = gains_amount
    blended_rate = 0.0

    # 0% portion: gains that fall below zero_max
    gains_in_zero = max(
        0.0, min(remaining_gains, thresholds.zero_max - income_below_gains)
    )
    gains_in_zero = max(0.0, gains_in_zero)
    remaining_gains -= gains_in_zero
    income_below_gains += gains_in_zero

    # 15% portion
    if remaining_gains > 0:
        gains_in_fifteen = max(
            0.0, min(remaining_gains, thresholds.fifteen_max - income_below_gains)
        )
        ltcg_tax += gains_in_fifteen * 0.15
        remaining_gains -= gains_in_fifteen
        income_below_gains += gains_in_fifteen

    # 20% portion
    if remaining_gains > 0:
        ltcg_tax += remaining_gains * 0.20

    # Net Investment Income Tax (3.8%) — on investment income above AGI threshold
    agi = ordinary_taxable_income + gains_amount  # simplified; no itemized here
    niit = 0.0
    if agi > thresholds.niit_threshold:
        investment_income_subject = min(
            gains_amount,
            agi - thresholds.niit_threshold,
        )
        niit = investment_income_subject * 0.038

    total_tax = ltcg_tax + niit
    blended_rate = total_tax / gains_amount if gains_amount > 0 else 0.0

    return LTCGTaxResult(
        gains_amount=gains_amount,
        ordinary_taxable_income=ordinary_taxable_income,
        rate_applied=blended_rate,
        ltcg_tax=ltcg_tax,
        niit=niit,
        total_tax=total_tax,
    )


def compute_total_tax(
    ordinary_income: float,
    ltcg_income: float,
    ss_benefits: float,
    tax_year: TaxYear,
    ltcg_thresholds: LTCGThresholds | None = None,
    include_bracket_detail: bool = False,
) -> TotalTaxResult:
    """
    Full tax computation for a retirement year combining all income sources.

    Income taxonomy:
        ordinary_income  = 401k/trad withdrawals + Roth conversions + wages
        ltcg_income      = brokerage account withdrawals (gains portion)
        ss_benefits      = gross Social Security (both spouses)
        → SS taxable portion is calculated here and added to ordinary income

    Args:
        ordinary_income:  Pre-SS ordinary taxable income.
        ltcg_income:      Long-term capital gains from brokerage.
        ss_benefits:      Total gross SS benefits.
        tax_year:         Tax brackets and standard deduction.
        ltcg_thresholds:  LTCG rate thresholds (inflated by caller if needed).

    Returns:
        TotalTaxResult with full breakdown.
    """
    ss_taxable = ss_taxable_amount(
        ss_gross=ss_benefits,
        other_income=ordinary_income + ltcg_income,
        filing_status=tax_year.filing_status,
    )

    total_ordinary = ordinary_income + ss_taxable

    ordinary_result = compute_ordinary_tax(
        gross_income=total_ordinary,
        tax_year=tax_year,
        include_bracket_detail=include_bracket_detail,
    )

    ltcg_result = compute_ltcg_tax(
        gains_amount=ltcg_income,
        ordinary_taxable_income=ordinary_result.taxable_income,
        thresholds=ltcg_thresholds,
    )

    total_tax = ordinary_result.tax_owed + ltcg_result.total_tax
    total_gross = ordinary_income + ltcg_income + ss_benefits
    total_effective = total_tax / total_gross if total_gross > 0 else 0.0

    return TotalTaxResult(
        ordinary=ordinary_result,
        ltcg=ltcg_result,
        total_tax=total_tax,
        total_effective_rate=total_effective,
        ss_taxable_amount=ss_taxable,
    )


# ---------------------------------------------------------------------------
# Optimizer helpers
# ---------------------------------------------------------------------------


def marginal_rate_at(income: float, tax_year: TaxYear) -> float:
    """
    Return the marginal bracket rate that applies to the next dollar of
    ordinary income at the given income level (after standard deduction).
    """
    taxable = max(0.0, income - tax_year.standard_deduction)
    brackets = sorted(tax_year.brackets, key=lambda b: b.income_min)
    rate = brackets[0].rate
    for bracket in brackets:
        if taxable >= bracket.income_min:
            rate = bracket.rate
    return rate


def bracket_fill_at(income: float, tax_year: TaxYear) -> BracketFill:
    """
    Calculate how much room remains in the current bracket and what comes next.
    Useful for Roth conversion optimizer to determine how much to convert
    before spilling into a higher bracket.

    Args:
        income:    Current ordinary income (gross, before std deduction).
        tax_year:  Tax year parameters.

    Returns:
        BracketFill with room remaining and next bracket info.
    """
    taxable = max(0.0, income - tax_year.standard_deduction)
    brackets = sorted(tax_year.brackets, key=lambda b: b.income_min)

    current_rate = brackets[0].rate
    current_max: float | None = None
    next_rate: float | None = None
    next_bracket_size: float | None = None

    for i, bracket in enumerate(brackets):
        b_max = bracket.income_max if bracket.income_max is not None else float("inf")
        if bracket.income_min <= taxable < b_max:
            current_rate = bracket.rate
            current_max = bracket.income_max
            if i + 1 < len(brackets):
                next_b = brackets[i + 1]
                next_rate = next_b.rate
                next_b_max = next_b.income_max or float("inf")
                next_bracket_size = next_b_max - next_b.income_min
            break

    remaining = (current_max - taxable) if current_max is not None else float("inf")

    return BracketFill(
        current_rate=current_rate,
        current_bracket_remaining=remaining,
        next_rate=next_rate,
        next_bracket_size=next_bracket_size,
    )


def roth_conversion_tax_cost(
    conversion_amount: float,
    existing_income: float,
    tax_year: TaxYear,
) -> float:
    """
    Calculate the incremental tax cost of a Roth conversion on top of
    existing income.  Used by the optimizer to price conversions.

    Args:
        conversion_amount: Amount to convert from traditional → Roth.
        existing_income:   Other ordinary income in the year.
        tax_year:          Tax year parameters.

    Returns:
        Incremental tax dollars owed due to the conversion.
    """
    tax_without = compute_ordinary_tax(existing_income, tax_year).tax_owed
    tax_with = compute_ordinary_tax(
        existing_income + conversion_amount, tax_year
    ).tax_owed
    return tax_with - tax_without
