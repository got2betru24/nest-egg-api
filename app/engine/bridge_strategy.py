# =============================================================================
# NestEgg - engine/bridge_strategy.py
# Early retirement bridge strategies for ages 55–59½.
#
# Covers:
#   - Rule of 55: penalty-free 401(k) withdrawals if separated at 55+
#   - 72(t) SEPP: Substantially Equal Periodic Payments from any IRA/401k
#   - Taxable bridge: HYSA + brokerage account drawdown
#   - Strategy selection and annual amounts for the bridge period
#
# The "bridge period" is the span from retirement until the later of:
#   - Age 59½ (penalty-free access to all retirement accounts)
#   - Social Security commencement
#
# All functions are pure math — no DB access.
# =============================================================================

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum


class BridgeMethod(str, Enum):
    RULE_OF_55 = "rule_of_55"
    SEPP_RMD = "sepp_rmd"               # 72(t) RMD method
    SEPP_FIXED_AMORTIZATION = "sepp_fixed_amortization"
    SEPP_FIXED_ANNUITIZATION = "sepp_fixed_annuitization"
    TAXABLE_ONLY = "taxable_only"       # HYSA + brokerage only
    MIXED = "mixed"                     # Combination


@dataclass
class BridgeInput:
    """Inputs needed to calculate bridge strategy amounts."""
    retirement_age: int                 # Age at retirement
    retirement_year: int                # Calendar year of retirement
    birth_year: int
    account_balance_401k: float         # Traditional 401k balance at retirement
    account_balance_ira: float          # Traditional IRA balance (if any)
    account_balance_hysa: float
    account_balance_brokerage: float
    annual_income_need: float           # Total gross income needed during bridge
    expected_return: float              # Expected annual return during bridge
    irs_interest_rate: float = 0.05     # 72(t) uses 120% of mid-term AFR; approximate


@dataclass
class SEPPCalculation:
    """Result of a 72(t) SEPP calculation."""
    method: BridgeMethod
    annual_amount: float
    monthly_amount: float
    account_balance_used: float
    modification_end_age: float         # Later of age 59½ or 5 years from start
    is_locked_in: bool = True           # SEPP cannot be changed without penalty


@dataclass
class BridgeYear:
    """Income sources for one year of the bridge period."""
    calendar_year: int
    age: int
    withdrawal_hysa: float
    withdrawal_brokerage: float
    withdrawal_401k_rule55: float
    withdrawal_sepp: float
    total_income: float
    strategy_used: BridgeMethod
    notes: list[str] = field(default_factory=list)


@dataclass
class BridgeStrategy:
    """Full bridge strategy recommendation."""
    method: BridgeMethod
    bridge_start_year: int
    bridge_end_year: int                # Year of age 59½ (approximately)
    bridge_years: list[BridgeYear]
    sepp_detail: SEPPCalculation | None
    total_taxable_withdrawn: float
    total_401k_withdrawn: float
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 72(t) SEPP calculations
# ---------------------------------------------------------------------------

def sepp_rmd_method(
    account_balance: float,
    age: int,
    life_expectancy_factor: float = 27.4,   # IRS Uniform Lifetime Table; 55yo ~ 29.6
) -> float:
    """
    72(t) Required Minimum Distribution method.
    Annual amount = account_balance / life_expectancy_factor.
    Amount recalculated annually (varies year to year).
    """
    return account_balance / life_expectancy_factor


# IRS Uniform Lifetime Table (abbreviated, ages 50–70)
_UNIFORM_LIFETIME_FACTORS = {
    50: 34.2, 51: 33.3, 52: 32.3, 53: 31.4, 54: 30.5,
    55: 29.6, 56: 28.7, 57: 27.9, 58: 27.0, 59: 26.1,
    60: 25.2, 61: 24.4, 62: 23.5, 63: 22.7, 64: 21.8,
    65: 21.0, 66: 20.2, 67: 19.4, 68: 18.6, 69: 17.8,
    70: 17.0,
}


def get_life_expectancy_factor(age: int) -> float:
    return _UNIFORM_LIFETIME_FACTORS.get(age, 27.4)


def sepp_fixed_amortization(
    account_balance: float,
    age: int,
    interest_rate: float,
    years_to_595: float,
) -> float:
    """
    72(t) Fixed Amortization method.
    Amortizes account balance over remaining life expectancy at a fixed rate.
    Amount is fixed for the duration of the SEPP plan.
    Often yields the highest annual amount of the three methods.

    Args:
        account_balance:  Balance in the SEPP account.
        age:              Age at SEPP commencement.
        interest_rate:    IRS-approved rate (≤ 120% of mid-term AFR).
        years_to_595:     Remaining years to the later of 59½ or 5 years.
    """
    life_expectancy = get_life_expectancy_factor(age)
    # Amortize over the greater of life expectancy or the SEPP lock-in period
    n = max(life_expectancy, years_to_595)
    r = interest_rate

    if r == 0:
        return account_balance / n

    # Standard amortization formula: PMT = PV * r / (1 - (1+r)^-n)
    annual = account_balance * r / (1 - (1 + r) ** (-n))
    return annual


def sepp_fixed_annuitization(
    account_balance: float,
    age: int,
    interest_rate: float,
) -> float:
    """
    72(t) Fixed Annuitization method.
    Uses an annuity factor based on IRS mortality tables and interest rate.
    Amount is fixed for the duration of the SEPP plan.
    Similar to amortization but uses a different annuity factor approach.
    """
    # Simplified annuity factor (IRS uses mortality tables; this is an approximation)
    life_expectancy = get_life_expectancy_factor(age)
    annuity_factor = (1 - (1 + interest_rate) ** (-life_expectancy)) / interest_rate
    return account_balance / annuity_factor


def compute_sepp(
    account_balance: float,
    retirement_age: int,
    retirement_year: int,
    birth_year: int,
    interest_rate: float = 0.05,
    method: BridgeMethod = BridgeMethod.SEPP_FIXED_AMORTIZATION,
) -> SEPPCalculation:
    """
    Compute a 72(t) SEPP plan for the given account and method.

    The SEPP plan must continue for the later of:
        - 5 years from the first payment
        - Until the account owner reaches age 59½

    Args:
        account_balance:   Balance in the IRA or 401k to use for SEPP.
        retirement_age:    Age when SEPP begins.
        retirement_year:   Calendar year SEPP begins.
        birth_year:        Person's birth year.
        interest_rate:     IRS-approved interest rate for the calculation.
        method:            Which 72(t) method to use.
    """
    age_595 = 59.5
    years_to_595 = max(age_595 - retirement_age, 0)
    modification_end_age = max(retirement_age + 5.0, age_595)

    if method == BridgeMethod.SEPP_RMD:
        factor = get_life_expectancy_factor(retirement_age)
        annual = sepp_rmd_method(account_balance, retirement_age, factor)
    elif method == BridgeMethod.SEPP_FIXED_AMORTIZATION:
        annual = sepp_fixed_amortization(
            account_balance, retirement_age, interest_rate, years_to_595
        )
    elif method == BridgeMethod.SEPP_FIXED_ANNUITIZATION:
        annual = sepp_fixed_annuitization(
            account_balance, retirement_age, interest_rate
        )
    else:
        raise ValueError(f"Unsupported SEPP method: {method}")

    return SEPPCalculation(
        method=method,
        annual_amount=round(annual, 2),
        monthly_amount=round(annual / 12, 2),
        account_balance_used=account_balance,
        modification_end_age=modification_end_age,
        is_locked_in=True,
    )


# ---------------------------------------------------------------------------
# Rule of 55
# ---------------------------------------------------------------------------

def rule_of_55_eligible(retirement_age: int) -> bool:
    """
    Returns True if the Rule of 55 applies.
    Requires separation from service in or after the year you turn 55.
    The 401(k) must be from the employer you're separating from.
    """
    return retirement_age >= 55


def rule_of_55_available_years(retirement_age: int) -> float:
    """
    Returns the number of years Rule of 55 applies before age 59½.
    After 59½, the normal penalty-free access rules take over.
    """
    return max(0.0, 59.5 - retirement_age)


# ---------------------------------------------------------------------------
# Bridge strategy builder
# ---------------------------------------------------------------------------

def build_bridge_strategy(
    inputs: BridgeInput,
    prefer_rule_of_55: bool = True,
    sepp_method: BridgeMethod = BridgeMethod.SEPP_FIXED_AMORTIZATION,
) -> BridgeStrategy:
    """
    Build the optimal bridge strategy for the years between retirement and 59½.

    Priority logic:
        1. Use HYSA + brokerage first (most flexible, no penalties or lock-ins).
        2. If Rule of 55 applies (retired at 55+), use 401k penalty-free.
        3. If gap remains, model SEPP from IRA or 401k.
        4. Roth contributions (not earnings) can always be withdrawn — modeled
           in the main projection engine, not here.

    Args:
        inputs:            Bridge calculation inputs.
        prefer_rule_of_55: Prefer Rule of 55 over SEPP when both available.
        sepp_method:       Which 72(t) calculation method to use if needed.

    Returns:
        BridgeStrategy with year-by-year plan.
    """
    bridge_years: list[BridgeYear] = []
    warnings: list[str] = []
    sepp_detail: SEPPCalculation | None = None

    retirement_age = inputs.retirement_age
    bridge_end_year = inputs.retirement_year + max(0, math.ceil(59.5 - retirement_age))
    r55_eligible = rule_of_55_eligible(retirement_age)

    # Running balances during bridge
    hysa_bal = inputs.account_balance_hysa
    brokerage_bal = inputs.account_balance_brokerage
    total_taxable_withdrawn = 0.0
    total_401k_withdrawn = 0.0

    sepp_annual: float = 0.0
    if not r55_eligible or not prefer_rule_of_55:
        # Model SEPP as fallback
        sepp_bal = inputs.account_balance_401k + inputs.account_balance_ira
        if sepp_bal > 0:
            sepp_detail = compute_sepp(
                account_balance=sepp_bal,
                retirement_age=retirement_age,
                retirement_year=inputs.retirement_year,
                birth_year=inputs.birth_year,
                interest_rate=inputs.irs_interest_rate,
                method=sepp_method,
            )
            sepp_annual = sepp_detail.annual_amount

    for offset in range(bridge_end_year - inputs.retirement_year + 1):
        cal_year = inputs.retirement_year + offset
        age = retirement_age + offset

        if age >= 60:
            break  # Past bridge period

        needed = inputs.annual_income_need
        withdrawal_hysa = 0.0
        withdrawal_brokerage = 0.0
        withdrawal_401k = 0.0
        withdrawal_sepp = 0.0
        notes: list[str] = []

        # Step 1: HYSA (most liquid, no tax complexity)
        if hysa_bal > 0 and needed > 0:
            take = min(hysa_bal, needed)
            withdrawal_hysa = take
            hysa_bal -= take
            needed -= take
            notes.append("HYSA drawdown")

        # Step 2: Brokerage (LTCG rates — favorable)
        if brokerage_bal > 0 and needed > 0:
            take = min(brokerage_bal, needed)
            withdrawal_brokerage = take
            brokerage_bal -= take
            needed -= take
            notes.append("Brokerage drawdown (LTCG rates)")

        # Step 3: 401k via Rule of 55 or SEPP
        if needed > 0:
            if r55_eligible and prefer_rule_of_55:
                withdrawal_401k = needed
                needed = 0.0
                notes.append("Rule of 55 (penalty-free 401k)")
                strategy = BridgeMethod.RULE_OF_55
            elif sepp_annual > 0:
                withdrawal_sepp = sepp_annual
                needed = max(0.0, needed - sepp_annual)
                notes.append(f"72(t) SEPP ({sepp_method.value})")
                strategy = sepp_method
            else:
                warnings.append(
                    f"Year {cal_year} (age {age}): income gap of ${needed:,.0f} "
                    f"— no penalty-free source available. "
                    f"Consider adjusting retirement age or increasing taxable savings."
                )
                strategy = BridgeMethod.TAXABLE_ONLY
        else:
            strategy = BridgeMethod.TAXABLE_ONLY if not r55_eligible else BridgeMethod.RULE_OF_55

        total = withdrawal_hysa + withdrawal_brokerage + withdrawal_401k + withdrawal_sepp
        total_taxable_withdrawn += withdrawal_hysa + withdrawal_brokerage
        total_401k_withdrawn += withdrawal_401k + withdrawal_sepp

        bridge_years.append(BridgeYear(
            calendar_year=cal_year,
            age=age,
            withdrawal_hysa=withdrawal_hysa,
            withdrawal_brokerage=withdrawal_brokerage,
            withdrawal_401k_rule55=withdrawal_401k,
            withdrawal_sepp=withdrawal_sepp,
            total_income=total,
            strategy_used=strategy,
            notes=notes,
        ))

    # Determine dominant strategy for summary
    if r55_eligible and prefer_rule_of_55:
        dominant = BridgeMethod.RULE_OF_55
    elif sepp_detail:
        dominant = sepp_method
    else:
        dominant = BridgeMethod.TAXABLE_ONLY

    return BridgeStrategy(
        method=dominant,
        bridge_start_year=inputs.retirement_year,
        bridge_end_year=bridge_end_year,
        bridge_years=bridge_years,
        sepp_detail=sepp_detail,
        total_taxable_withdrawn=total_taxable_withdrawn,
        total_401k_withdrawn=total_401k_withdrawn,
        warnings=warnings,
    )
