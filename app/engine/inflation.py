# =============================================================================
# NestEgg - engine/inflation.py
# Nominal/real conversion helpers and inflation-adjusted income targeting.
#
# Design philosophy:
#   - The projection engine runs in NOMINAL dollars (account balances show
#     their actual future value, comparable to other tools).
#   - User inputs desired retirement income in TODAY'S dollars.
#   - This module converts that to a nominal income target for each future year.
#   - All functions are pure (no side effects, no I/O).
# =============================================================================

from __future__ import annotations


def inflate(amount: float, rate: float, years: int) -> float:
    """
    Grow `amount` by `rate` for `years` periods (compound).

    Args:
        amount: Base amount in today's dollars.
        rate:   Annual inflation rate (e.g. 0.03 for 3%).
        years:  Number of years to inflate.

    Returns:
        Nominal future value.

    Example:
        >>> inflate(100_000, 0.03, 10)
        134391.64...
    """
    if years < 0:
        raise ValueError(f"years must be >= 0, got {years}")
    return amount * (1 + rate) ** years


def deflate(amount: float, rate: float, years: int) -> float:
    """
    Discount a future nominal `amount` back to today's dollars.

    Args:
        amount: Nominal future amount.
        rate:   Annual inflation rate (e.g. 0.03 for 3%).
        years:  Number of years to discount.

    Returns:
        Present (real) value.
    """
    if years < 0:
        raise ValueError(f"years must be >= 0, got {years}")
    if rate == -1.0:
        raise ValueError("Inflation rate cannot be -100%")
    return amount / (1 + rate) ** years


def real_return(nominal_return: float, inflation_rate: float) -> float:
    """
    Convert a nominal return to a real (inflation-adjusted) return using
    the Fisher equation.

    Args:
        nominal_return: Nominal annual return (e.g. 0.07).
        inflation_rate: Annual inflation rate (e.g. 0.03).

    Returns:
        Real annual return.

    Example:
        >>> real_return(0.07, 0.03)
        0.03883...
    """
    return (1 + nominal_return) / (1 + inflation_rate) - 1


def nominal_income_target(
    today_dollars: float,
    inflation_rate: float,
    years_until_retirement: int,
    years_into_retirement: int,
) -> float:
    """
    Calculate the nominal income needed in a specific retirement year,
    starting from a today's-dollar income target.

    The income target inflates from now through the entire period
    (accumulation + years already into retirement).

    Args:
        today_dollars:           Desired retirement income in today's dollars.
        inflation_rate:          Annual inflation rate.
        years_until_retirement:  Years from now until retirement starts.
        years_into_retirement:   How many years past retirement start (0 = first year).

    Returns:
        Nominal income target for that specific retirement year.
    """
    total_years = years_until_retirement + years_into_retirement
    return inflate(today_dollars, inflation_rate, total_years)


def build_income_schedule(
    today_dollars: float,
    inflation_rate: float,
    years_until_retirement: int,
    retirement_duration: int,
) -> list[float]:
    """
    Build the full list of nominal income targets for each year of retirement.

    Args:
        today_dollars:           Desired retirement income in today's dollars.
        inflation_rate:          Annual inflation rate.
        years_until_retirement:  Years from now until retirement.
        retirement_duration:     Number of retirement years to model.

    Returns:
        List of length `retirement_duration` with nominal income target per year.
        Index 0 = first year of retirement.
    """
    return [
        nominal_income_target(
            today_dollars,
            inflation_rate,
            years_until_retirement,
            yr,
        )
        for yr in range(retirement_duration)
    ]


def today_dollars(nominal_amount: float, inflation_rate: float, years: int) -> float:
    """
    Convenience alias for deflate().  Returns the real purchasing-power
    equivalent of a future nominal amount.
    """
    return deflate(nominal_amount, inflation_rate, years)
