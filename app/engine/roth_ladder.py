# =============================================================================
# NestEgg - engine/roth_ladder.py
# Roth conversion ladder scheduling and 5-year seasoning rule tracking.
#
# A Roth conversion ladder is a strategy to systematically convert
# traditional 401k / IRA funds to Roth during low-income years (typically
# early retirement before SS and other income begins), paying income tax
# at favorable rates now to enable tax-free growth and withdrawals later.
#
# Key rules modeled here:
#   - Each conversion has its own 5-year clock before principal can be
#     withdrawn penalty-free (if under 59½).
#   - After 59½, all conversions and earnings are accessible penalty-free
#     (assuming the Roth account itself is 5+ years old).
#   - Conversions are taxed as ordinary income in the year of conversion.
#   - The optimizer fills tax brackets up to a target bracket ceiling.
#
# All functions are pure math — no DB access.
# =============================================================================

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .tax_engine import (
    TaxYear,
    bracket_fill_at,
    roth_conversion_tax_cost,
)


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------

class ConversionStatus(str, Enum):
    SEASONING = "seasoning"         # Within 5-year window (under 59½ only)
    AVAILABLE = "available"         # Past 5-year window OR age 59½+
    FUTURE = "future"               # Not yet converted


@dataclass
class ConversionRecord:
    """Tracks one year's Roth conversion and its seasoning status."""
    conversion_year: int
    amount: float
    tax_cost: float
    available_year: int             # Year the converted principal becomes accessible
    status: ConversionStatus = ConversionStatus.SEASONING


@dataclass
class LadderState:
    """
    Full state of the Roth conversion ladder at any point in time.
    Maintained year over year by the projection engine.
    """
    conversions: list[ConversionRecord] = field(default_factory=list)

    def available_principal(self, current_year: int, age: float) -> float:
        """Total converted principal accessible without penalty in current_year."""
        if age >= 59.5:
            return sum(c.amount for c in self.conversions)
        return sum(
            c.amount for c in self.conversions
            if c.available_year <= current_year
        )

    def seasoning_principal(self, current_year: int, age: float) -> float:
        """Converted principal still within the 5-year window."""
        if age >= 59.5:
            return 0.0
        return sum(
            c.amount for c in self.conversions
            if c.available_year > current_year
        )

    def total_converted(self) -> float:
        return sum(c.amount for c in self.conversions)


@dataclass
class ConversionYearResult:
    """Result of planning one year's conversion."""
    year: int
    age: int
    existing_income: float              # Income before conversion
    conversion_amount: float            # Amount converted this year
    tax_cost: float                     # Incremental tax from conversion
    marginal_rate_used: float           # Rate at which conversion was taxed
    bracket_ceiling_used: float         # Target bracket rate (optimizer)
    room_used: float                    # How much bracket room was consumed
    available_at_year: int              # Year converted principal is accessible
    note: str = ""


@dataclass
class LadderSchedule:
    """Full multi-year Roth conversion ladder plan."""
    conversion_years: list[ConversionYearResult]
    total_converted: float
    total_tax_cost: float
    average_effective_rate: float
    ladder_state: LadderState


# ---------------------------------------------------------------------------
# Optimizer: how much to convert in a given year
# ---------------------------------------------------------------------------

def optimal_conversion_amount(
    existing_income: float,
    traditional_balance: float,
    tax_year: TaxYear,
    target_bracket_ceiling: float = 0.22,   # Don't spill into brackets above this
    max_conversion: float | None = None,
) -> float:
    """
    Calculate the optimal Roth conversion amount for a given year.

    Fills the current bracket up to (but not exceeding) the target ceiling.
    Converts as much of the traditional balance as possible within that limit.

    Args:
        existing_income:          Other income in the year (SS, other withdrawals).
        traditional_balance:      Remaining traditional 401k / IRA balance.
        tax_year:                 Tax brackets and deductions.
        target_bracket_ceiling:   Maximum marginal rate to convert at (e.g. 0.22).
        max_conversion:           Hard cap on conversion amount (e.g. user override).

    Returns:
        Optimal conversion amount (may be 0 if already at or above ceiling).
    """
    fill = bracket_fill_at(existing_income, tax_year)

    # If already at or above ceiling, don't convert
    if fill.current_rate >= target_bracket_ceiling:
        return 0.0

    # Room in current bracket before hitting next bracket
    room_in_current = fill.current_bracket_remaining

    # If next bracket is at or above ceiling, we can only fill current bracket
    if fill.next_rate is not None and fill.next_rate >= target_bracket_ceiling:
        optimal = room_in_current
    else:
        # We can also fill the next bracket if it's below ceiling
        # (Rare — usually we stop at first bracket transition above ceiling)
        optimal = room_in_current

    # Cap by available traditional balance
    optimal = min(optimal, traditional_balance)

    # Apply user cap if set
    if max_conversion is not None:
        optimal = min(optimal, max_conversion)

    return max(0.0, optimal)


# ---------------------------------------------------------------------------
# Single year conversion execution
# ---------------------------------------------------------------------------

def execute_conversion(
    year: int,
    age: int,
    existing_income: float,
    amount: float,
    traditional_balance: float,
    tax_year: TaxYear,
) -> ConversionYearResult:
    """
    Execute a Roth conversion for a specific year.

    Args:
        year:               Calendar year of conversion.
        age:                Person's age in this year.
        existing_income:    Other ordinary income in this year.
        amount:             Conversion amount (may be clamped to balance).
        traditional_balance: Available traditional balance.
        tax_year:           Tax parameters.

    Returns:
        ConversionYearResult with tax cost and seasoning details.
    """
    actual_amount = min(amount, traditional_balance)
    if actual_amount <= 0:
        return ConversionYearResult(
            year=year,
            age=age,
            existing_income=existing_income,
            conversion_amount=0.0,
            tax_cost=0.0,
            marginal_rate_used=0.0,
            bracket_ceiling_used=0.0,
            room_used=0.0,
            available_at_year=year + 5,
            note="No conversion — balance insufficient or amount is zero",
        )

    tax_cost = roth_conversion_tax_cost(actual_amount, existing_income, tax_year)
    fill = bracket_fill_at(existing_income, tax_year)
    available_year = year + 5   # 5-year seasoning rule (irrelevant after 59½)

    return ConversionYearResult(
        year=year,
        age=age,
        existing_income=existing_income,
        conversion_amount=actual_amount,
        tax_cost=tax_cost,
        marginal_rate_used=fill.current_rate,
        bracket_ceiling_used=fill.next_rate or fill.current_rate,
        room_used=fill.current_bracket_remaining,
        available_at_year=available_year,
        note=(
            f"Converted ${actual_amount:,.0f} at "
            f"{fill.current_rate:.0%} marginal rate; "
            f"available penalty-free in {available_year}"
        ),
    )


# ---------------------------------------------------------------------------
# Multi-year ladder schedule builder
# ---------------------------------------------------------------------------

def build_ladder_schedule(
    retirement_year: int,
    retirement_age: int,
    ss_start_year: int,                      # Year SS income begins (reduces room)
    traditional_balance_at_retirement: float,
    annual_return: float,                    # For projecting growing balance
    income_schedule: dict[int, float],       # {year: other_income} during ladder years
    tax_year_factory: dict[int, TaxYear],    # {year: TaxYear} — caller provides
    target_bracket_ceiling: float = 0.22,
    max_ladder_years: int = 15,
    user_overrides: dict[int, float] | None = None,  # {year: amount} manual overrides
) -> LadderSchedule:
    """
    Build a multi-year Roth conversion ladder schedule.

    Runs from retirement until SS commences (or max_ladder_years), converting
    as much as possible each year within the target bracket ceiling.

    Args:
        retirement_year:                  Calendar year of retirement.
        retirement_age:                   Age at retirement.
        ss_start_year:                    Year SS income begins (shrinks bracket room).
        traditional_balance_at_retirement: 401k balance at retirement.
        annual_return:                    Expected return on remaining balance.
        income_schedule:                  Other income by year during ladder.
        tax_year_factory:                 TaxYear objects keyed by calendar year.
        target_bracket_ceiling:           Max bracket rate for conversions.
        max_ladder_years:                 Safety cap on ladder duration.
        user_overrides:                   Manual conversion amounts by year.

    Returns:
        LadderSchedule with full year-by-year detail.
    """
    ladder_state = LadderState()
    results: list[ConversionYearResult] = []
    balance = traditional_balance_at_retirement
    overrides = user_overrides or {}

    end_year = min(
        retirement_year + max_ladder_years,
        ss_start_year,  # Stop aggressive conversion when SS starts (fills brackets)
    )

    for offset in range(end_year - retirement_year):
        cal_year = retirement_year + offset
        age = retirement_age + offset

        if balance <= 0:
            break

        tax_year = tax_year_factory.get(cal_year)
        if tax_year is None:
            # Fall back to most recent available year
            max_year = max(tax_year_factory.keys())
            tax_year = tax_year_factory[max_year]

        existing_income = income_schedule.get(cal_year, 0.0)

        if cal_year in overrides:
            amount = overrides[cal_year]
        else:
            amount = optimal_conversion_amount(
                existing_income=existing_income,
                traditional_balance=balance,
                tax_year=tax_year,
                target_bracket_ceiling=target_bracket_ceiling,
            )

        result = execute_conversion(
            year=cal_year,
            age=age,
            existing_income=existing_income,
            amount=amount,
            traditional_balance=balance,
            tax_year=tax_year,
        )

        if result.conversion_amount > 0:
            balance -= result.conversion_amount
            # Grow remaining balance for next year
            balance *= (1 + annual_return)

            ladder_state.conversions.append(ConversionRecord(
                conversion_year=cal_year,
                amount=result.conversion_amount,
                tax_cost=result.tax_cost,
                available_year=result.available_at_year,
                status=ConversionStatus.SEASONING,
            ))
        else:
            balance *= (1 + annual_return)

        results.append(result)

    total_converted = sum(r.conversion_amount for r in results)
    total_tax = sum(r.tax_cost for r in results)
    avg_rate = total_tax / total_converted if total_converted > 0 else 0.0

    return LadderSchedule(
        conversion_years=results,
        total_converted=total_converted,
        total_tax_cost=total_tax,
        average_effective_rate=avg_rate,
        ladder_state=ladder_state,
    )


# ---------------------------------------------------------------------------
# Seasoning status updater (called each projection year)
# ---------------------------------------------------------------------------

def update_seasoning(
    ladder_state: LadderState,
    current_year: int,
    current_age: float,
) -> LadderState:
    """
    Update ConversionStatus for all records in the ladder state.
    Called each year in the projection engine.
    """
    for record in ladder_state.conversions:
        if current_age >= 59.5 or record.available_year <= current_year:
            record.status = ConversionStatus.AVAILABLE
        else:
            record.status = ConversionStatus.SEASONING
    return ladder_state
