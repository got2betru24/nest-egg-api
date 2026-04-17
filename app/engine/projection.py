# =============================================================================
# NestEgg - engine/projection.py
# Year-by-year account projection engine.
#
# Orchestrates all engine modules into a single timeline of account balances,
# income, taxes, contributions, and withdrawals from today through plan_to_age.
#
# Phase separation:
#   ACCUMULATION: From today until retirement. Accounts grow, contributions
#                 are made, no withdrawals.
#   BRIDGE:       Retirement to age 59½. Taxable accounts + Rule55/SEPP fund
#                 income needs. Roth ladder conversions begin.
#   DISTRIBUTION: Age 59½ onward. Optimizer drives withdrawal order. SS begins
#                 at configured claiming age. Roth ladder conversions continue
#                 as long as traditional balance and bracket room allow.
#
# All inputs are passed in as dataclasses — no DB access.
# =============================================================================

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .contribution_limits import LimitRow, get_limits
from .inflation import inflate, build_income_schedule
from .roth_ladder import LadderState, update_seasoning
from .social_security import SSBenefitEstimate, annual_benefit_in_year
from .tax_engine import (
    LTCGThresholds,
    TaxYear,
    TotalTaxResult,
    compute_total_tax,
)


# ---------------------------------------------------------------------------
# Enums and phase markers
# ---------------------------------------------------------------------------


class ProjectionPhase(str, Enum):
    ACCUMULATION = "accumulation"
    BRIDGE = "bridge"
    DISTRIBUTION = "distribution"


class ReturnScenario(str, Enum):
    CONSERVATIVE = "conservative"
    BASE = "base"
    OPTIMISTIC = "optimistic"


# ---------------------------------------------------------------------------
# Input structures
# ---------------------------------------------------------------------------


@dataclass
class AccountInputs:
    """Current balances and return assumptions for all accounts."""

    hysa_balance: float = 0.0
    brokerage_balance: float = 0.0
    roth_ira_balance: float = 0.0
    traditional_401k_balance: float = 0.0
    roth_401k_balance: float = 0.0

    # Returns per scenario (nominal)
    hysa_return_conservative: float = 0.02
    hysa_return_base: float = 0.04
    hysa_return_optimistic: float = 0.05

    brokerage_return_conservative: float = 0.05
    brokerage_return_base: float = 0.07
    brokerage_return_optimistic: float = 0.10

    roth_ira_return_conservative: float = 0.05
    roth_ira_return_base: float = 0.07
    roth_ira_return_optimistic: float = 0.10

    traditional_401k_return_conservative: float = 0.05
    traditional_401k_return_base: float = 0.07
    traditional_401k_return_optimistic: float = 0.10

    roth_401k_return_conservative: float = 0.05
    roth_401k_return_base: float = 0.07
    roth_401k_return_optimistic: float = 0.10

    def returns(self, scenario: ReturnScenario) -> dict[str, float]:
        s = scenario.value
        return {
            "hysa": getattr(self, f"hysa_return_{s}"),
            "brokerage": getattr(self, f"brokerage_return_{s}"),
            "roth_ira": getattr(self, f"roth_ira_return_{s}"),
            "traditional_401k": getattr(self, f"traditional_401k_return_{s}"),
            "roth_401k": getattr(self, f"roth_401k_return_{s}"),
        }


@dataclass
class ContributionInputs:
    """Annual contribution amounts (employee only; employer match separate)."""

    traditional_401k_annual: float = 0.0
    roth_401k_annual: float = 0.0
    roth_ira_annual: float = 0.0
    employer_match_annual: float = 0.0
    hysa_annual: float = 0.0
    brokerage_annual: float = 0.0
    enable_catchup: bool = False
    solve_mode: str = "fixed"  # 'fixed' | 'solve_for'


@dataclass
class PersonInputs:
    """Demographics and SS for one person."""

    birth_year: int
    retirement_age: int
    ss_benefit: SSBenefitEstimate | None  # Pre-computed; None = no SS
    ss_claim_start_year: int | None  # Calendar year SS begins
    ss_cola_rows: list[dict] = field(default_factory=list)
    assumed_future_ss_cola: float = 0.025


@dataclass
class ProjectionInputs:
    """All inputs for a full projection run."""

    current_year: int
    primary: PersonInputs
    spouse: PersonInputs | None

    accounts: AccountInputs
    contributions: ContributionInputs
    limit_rows: list[LimitRow]  # From DB for current year

    # Income & lifestyle
    desired_retirement_income_today: float
    current_income: float          # Combined household income (legacy; kept for contribution planner)
    primary_income: float = 0.0   # Primary's individual earned income (SS projection)
    spouse_income: float = 0.0    # Spouse's individual earned income (SS projection)
    inflation_rate: float = 0.03
    plan_to_age: int = 90
    healthcare_annual_cost: float = 0.0  # Pre-Medicare bridge cost

    # Tax
    tax_years: dict[int, TaxYear] = field(default_factory=dict)
    ltcg_thresholds: LTCGThresholds = field(default_factory=LTCGThresholds)

    # Roth ladder
    enable_roth_ladder: bool = True
    roth_ladder_target_bracket: float = 0.22

    # Roth ladder user overrides {calendar_year: amount}
    roth_ladder_overrides: dict[int, float] = field(default_factory=dict)

    # Withdrawal order override (list of account names in draw order)
    # Only used when withdrawal_strategy == 'static'.
    withdrawal_order: list[str] | None = None

    # 'dynamic' = bracket-aware per-year optimizer (default, spend-maximizing)
    # 'static'  = legacy fixed waterfall (for regression testing / comparison)
    withdrawal_strategy: str = "dynamic"


# ---------------------------------------------------------------------------
# Output structures
# ---------------------------------------------------------------------------


@dataclass
class AccountBalances:
    hysa: float = 0.0
    brokerage: float = 0.0
    roth_ira: float = 0.0
    traditional_401k: float = 0.0
    roth_401k: float = 0.0

    @property
    def total_pretax(self) -> float:
        return self.traditional_401k

    @property
    def total_posttax(self) -> float:
        return self.hysa + self.brokerage + self.roth_ira + self.roth_401k

    @property
    def total(self) -> float:
        return self.total_pretax + self.total_posttax


@dataclass
class YearWithdrawals:
    hysa: float = 0.0
    brokerage: float = 0.0
    roth_ira: float = 0.0
    traditional_401k: float = 0.0
    roth_401k: float = 0.0
    roth_conversion: float = 0.0


@dataclass
class YearContributions:
    traditional_401k: float = 0.0
    roth_401k: float = 0.0
    roth_ira: float = 0.0
    employer_match: float = 0.0
    hysa: float = 0.0
    brokerage: float = 0.0


@dataclass
class ProjectionYear:
    """Full detail for one year of the projection."""

    calendar_year: int
    age_primary: int
    age_spouse: int | None
    phase: ProjectionPhase

    balances_start: AccountBalances
    balances_end: AccountBalances

    contributions: YearContributions
    withdrawals: YearWithdrawals

    ss_primary: float = 0.0
    ss_spouse: float = 0.0
    healthcare_cost: float = 0.0

    tax_result: TotalTaxResult | None = None
    gross_income: float = 0.0
    net_income: float = 0.0
    income_target: float = 0.0
    income_gap: float = 0.0  # Negative = shortfall

    roth_ladder_conversion: float = 0.0
    roth_available_principal: float = 0.0

    is_depleted: bool = False  # True if portfolio ran out
    notes: list[str] = field(default_factory=list)


@dataclass
class ProjectionResult:
    """Full projection output."""

    scenario: ReturnScenario
    years: list[ProjectionYear]
    depletion_year: int | None  # Calendar year portfolio hits zero
    depletion_age: int | None
    final_balance: float
    total_tax_paid: float
    total_ss_received: float
    success: bool  # True = portfolio survived to plan_to_age


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_tax_year(tax_years: dict[int, TaxYear], year: int) -> TaxYear:
    """Get TaxYear for a given calendar year, falling back to most recent."""
    if year in tax_years:
        return tax_years[year]
    max_year = max(tax_years.keys())
    return tax_years[max_year]


def _grow(balance: float, rate: float) -> float:
    return balance * (1 + rate)


def _apply_contributions(
    balances: AccountBalances,
    contribs: YearContributions,
) -> AccountBalances:
    return AccountBalances(
        hysa=balances.hysa + contribs.hysa,
        brokerage=balances.brokerage + contribs.brokerage,
        roth_ira=balances.roth_ira + contribs.roth_ira,
        traditional_401k=balances.traditional_401k
        + contribs.traditional_401k
        + contribs.employer_match,
        roth_401k=balances.roth_401k + contribs.roth_401k,
    )


def _default_withdrawal_order(age: float, has_bridge: bool) -> list[str]:
    """
    Legacy static withdrawal order — used only when withdrawal_strategy == 'static'.
    During bridge (< 59.5): taxable first, then Rule of 55 / SEPP.
    Post-59.5: taxable → traditional (fills brackets) → Roth (last resort).
    """
    if age < 59.5:
        return ["hysa", "brokerage", "traditional_401k", "roth_401k", "roth_ira"]
    return ["hysa", "brokerage", "traditional_401k", "roth_401k", "roth_ira"]


def _withdraw_from_accounts(
    needed: float,
    balances: AccountBalances,
    order: list[str],
    roth_available_principal: float,
) -> tuple[AccountBalances, YearWithdrawals, float]:
    """
    Legacy static waterfall — used only when withdrawal_strategy == 'static'.
    Withdraw `needed` from accounts in the specified order.
    """
    w = YearWithdrawals()
    bal = AccountBalances(
        hysa=balances.hysa,
        brokerage=balances.brokerage,
        roth_ira=balances.roth_ira,
        traditional_401k=balances.traditional_401k,
        roth_401k=balances.roth_401k,
    )
    remaining = needed

    for account in order:
        if remaining <= 0:
            break

        if account == "hysa":
            take = min(bal.hysa, remaining)
            w.hysa += take
            bal.hysa -= take
            remaining -= take

        elif account == "brokerage":
            take = min(bal.brokerage, remaining)
            w.brokerage += take
            bal.brokerage -= take
            remaining -= take

        elif account == "traditional_401k":
            take = min(bal.traditional_401k, remaining)
            w.traditional_401k += take
            bal.traditional_401k -= take
            remaining -= take

        elif account == "roth_401k":
            available = bal.roth_401k
            take = min(available, remaining)
            w.roth_401k += take
            bal.roth_401k -= take
            remaining -= take

        elif account == "roth_ira":
            available = min(bal.roth_ira, roth_available_principal)
            take = min(available, remaining)
            w.roth_ira += take
            bal.roth_ira -= take
            remaining -= take

    return bal, w, remaining


def _smart_withdraw_and_convert(
    needed: float,
    balances: AccountBalances,
    ss_income: float,
    tax_year: TaxYear,
    ltcg_thresholds: LTCGThresholds,
    roth_available_principal: float,
    enable_roth_ladder: bool,
    roth_ladder_target_bracket: float,
    roth_ladder_override: float | None,
    traditional_balance: float,
    ladder_state: LadderState,
    cal_year: int,
    age_primary: int,
    is_bridge: bool,
) -> tuple[AccountBalances, YearWithdrawals, float, float, list[str]]:
    """
    Bracket-aware, spend-maximizing withdrawal and Roth conversion optimizer.

    Jointly decides how much to withdraw from each account AND how much to
    Roth-convert each year, using shared bracket room optimally.

    Algorithm:
        1. Compute base ordinary income floor (SS taxable estimate).
        2. Determine ordinary bracket room up to target ceiling.
        3. Pull traditional 401k to meet portfolio need, capped at bracket room.
        4. Place brokerage before traditional when doing so stays in 0% LTCG zone.
        5. After withdrawal need is met, fill remaining bracket room with
           Roth conversion (the ladder) — conversions never displace withdrawals.
        6. Fill any remaining need from zero-cost sources: HYSA first, then Roth.
        7. Spill into next bracket from traditional only if all else is exhausted.

    Returns:
        (updated_balances, withdrawals, roth_conversion_amount, remaining_needed, notes)
    """
    from .roth_ladder import (
        ConversionRecord,
        execute_conversion,
        optimal_conversion_amount,
    )
    from .tax_engine import bracket_fill_at, ss_taxable_amount

    notes: list[str] = []
    w = YearWithdrawals()
    bal = AccountBalances(
        hysa=balances.hysa,
        brokerage=balances.brokerage,
        roth_ira=balances.roth_ira,
        traditional_401k=balances.traditional_401k,
        roth_401k=balances.roth_401k,
    )
    roth_conversion = 0.0
    remaining = needed

    # ------------------------------------------------------------------
    # Step 1: Establish income floor — SS taxable portion.
    # We use the real formula here rather than the 0.85 approximation.
    # ------------------------------------------------------------------
    ss_taxable_floor = ss_taxable_amount(
        ss_gross=ss_income,
        other_income=0.0,  # Conservative: compute floor with no other income yet
        filing_status=tax_year.filing_status,
    )
    income_on_table = ss_taxable_floor  # grows as we add withdrawals below

    # ------------------------------------------------------------------
    # Step 2: How much ordinary bracket room do we have before hitting
    # the Roth ladder's target ceiling?  This is shared between trad
    # 401k withdrawals and any Roth conversion.
    # ------------------------------------------------------------------
    fill = bracket_fill_at(income_on_table, tax_year)
    at_or_above_ceiling = fill.current_rate >= roth_ladder_target_bracket
    ordinary_room = fill.current_bracket_remaining if not at_or_above_ceiling else 0.0

    # ------------------------------------------------------------------
    # Step 3: Brokerage — take it BEFORE traditional when the combined
    # ordinary + gains income stays within the 0% LTCG zone.
    # After 59½ only (bridge phase uses taxable-first anyway via is_bridge).
    # ------------------------------------------------------------------
    if bal.brokerage > 0 and remaining > 0 and not is_bridge:
        # How much brokerage can we pull at 0% LTCG?
        # 0% zone: total taxable income (ordinary + gains) < ltcg_thresholds.zero_max
        # Ordinary so far is income_on_table. Gains stack on top.
        ltcg_zero_room = max(
            0.0,
            ltcg_thresholds.zero_max
            - max(0.0, income_on_table - tax_year.standard_deduction),
        )
        # Pull up to min(need, brokerage balance, 0% LTCG room)
        brokerage_at_zero = min(bal.brokerage, remaining, ltcg_zero_room)
        if brokerage_at_zero > 0:
            w.brokerage += brokerage_at_zero
            bal.brokerage -= brokerage_at_zero
            remaining -= brokerage_at_zero
            notes.append(
                f"Brokerage ${brokerage_at_zero:,.0f} at 0% LTCG"
                if brokerage_at_zero > 0
                else ""
            )

    # ------------------------------------------------------------------
    # Step 4: Traditional 401k — fill up to ordinary bracket room.
    # This is the "cheap" traditional withdrawal zone.
    # ------------------------------------------------------------------
    if bal.traditional_401k > 0 and remaining > 0 and ordinary_room > 0:
        trad_at_low_rate = min(bal.traditional_401k, remaining, ordinary_room)
        w.traditional_401k += trad_at_low_rate
        bal.traditional_401k -= trad_at_low_rate
        remaining -= trad_at_low_rate
        income_on_table += trad_at_low_rate
        ordinary_room -= trad_at_low_rate

    # ------------------------------------------------------------------
    # Step 5: HYSA — zero tax cost, spend before Roth to preserve
    # tax-free Roth growth.
    # ------------------------------------------------------------------
    if bal.hysa > 0 and remaining > 0:
        take = min(bal.hysa, remaining)
        w.hysa += take
        bal.hysa -= take
        remaining -= take

    # ------------------------------------------------------------------
    # Step 6: Roth — last free source before spilling into higher brackets.
    # ------------------------------------------------------------------
    if remaining > 0:
        roth_total_available = (
            bal.roth_ira + bal.roth_401k
            if age_primary >= 60
            else min(roth_available_principal, bal.roth_ira) + bal.roth_401k
        )
        if roth_total_available > 0:
            # Draw from Roth 401k first (often smaller, preserve Roth IRA growth)
            take_401k = min(bal.roth_401k, remaining)
            w.roth_401k += take_401k
            bal.roth_401k -= take_401k
            remaining -= take_401k

            if remaining > 0:
                ira_available = (
                    bal.roth_ira
                    if age_primary >= 60
                    else min(roth_available_principal, bal.roth_ira)
                )
                take_ira = min(ira_available, remaining)
                w.roth_ira += take_ira
                bal.roth_ira -= take_ira
                remaining -= take_ira

    # ------------------------------------------------------------------
    # Step 7: Spill — if still short, pull more traditional (higher bracket)
    # and any remaining brokerage at 15%+ LTCG.
    # ------------------------------------------------------------------
    if remaining > 0 and bal.traditional_401k > 0:
        take = min(bal.traditional_401k, remaining)
        w.traditional_401k += take
        bal.traditional_401k -= take
        remaining -= take
        income_on_table += take
        notes.append(f"Bracket spill: ${take:,.0f} trad 401k at higher rate")

    if remaining > 0 and bal.brokerage > 0:
        take = min(bal.brokerage, remaining)
        w.brokerage += take
        bal.brokerage -= take
        remaining -= take

    # ------------------------------------------------------------------
    # Step 8: Roth conversion — use any remaining bracket room AFTER
    # withdrawals are fully resolved. Conversions never displace income
    # needs; they only use leftover room.
    # ------------------------------------------------------------------
    if enable_roth_ladder and bal.traditional_401k > 0:
        # Recompute bracket position now that withdrawals have consumed room
        fill_post = bracket_fill_at(income_on_table, tax_year)
        remaining_ordinary_room = (
            fill_post.current_bracket_remaining
            if fill_post.current_rate < roth_ladder_target_bracket
            else 0.0
        )

        if roth_ladder_override is not None:
            conv_amount = roth_ladder_override
        else:
            conv_amount = optimal_conversion_amount(
                existing_income=income_on_table,
                traditional_balance=bal.traditional_401k,
                tax_year=tax_year,
                target_bracket_ceiling=roth_ladder_target_bracket,
            )

        if conv_amount > 0:
            conv_result = execute_conversion(
                year=cal_year,
                age=age_primary,
                existing_income=income_on_table,
                amount=conv_amount,
                traditional_balance=bal.traditional_401k,
                tax_year=tax_year,
            )
            roth_conversion = conv_result.conversion_amount
            bal.traditional_401k -= roth_conversion
            bal.roth_ira += roth_conversion
            w.roth_conversion = roth_conversion
            ladder_state.conversions.append(
                ConversionRecord(
                    conversion_year=cal_year,
                    amount=roth_conversion,
                    tax_cost=conv_result.tax_cost,
                    available_year=conv_result.available_at_year,
                )
            )
            notes.append(f"Roth conversion: ${roth_conversion:,.0f} (leftover bracket room)")

    return bal, w, roth_conversion, remaining, notes


# ---------------------------------------------------------------------------
# Main projection engine
# ---------------------------------------------------------------------------


def run_projection(
    inputs: ProjectionInputs,
    scenario: ReturnScenario = ReturnScenario.BASE,
) -> ProjectionResult:
    """
    Run a full year-by-year projection from current year to plan_to_age.

    Args:
        inputs:   All projection inputs.
        scenario: Return scenario to use.

    Returns:
        ProjectionResult with full year-by-year detail.
    """
    returns = inputs.accounts.returns(scenario)
    primary = inputs.primary
    spouse = inputs.spouse

    retirement_year = inputs.current_year + (
        primary.retirement_age - (inputs.current_year - primary.birth_year)
    )
    plan_end_year = primary.birth_year + inputs.plan_to_age
    years_until_retirement = retirement_year - inputs.current_year
    retirement_duration = plan_end_year - retirement_year

    income_schedule = build_income_schedule(
        today_dollars=inputs.desired_retirement_income_today,
        inflation_rate=inputs.inflation_rate,
        years_until_retirement=years_until_retirement,
        retirement_duration=retirement_duration,
    )

    # Initialize balances
    balances = AccountBalances(
        hysa=inputs.accounts.hysa_balance,
        brokerage=inputs.accounts.brokerage_balance,
        roth_ira=inputs.accounts.roth_ira_balance,
        traditional_401k=inputs.accounts.traditional_401k_balance,
        roth_401k=inputs.accounts.roth_401k_balance,
    )

    ladder_state = LadderState()
    projection_years: list[ProjectionYear] = []
    total_tax_paid = 0.0
    total_ss_received = 0.0
    depletion_year: int | None = None
    depletion_age: int | None = None

    for cal_year in range(inputs.current_year, plan_end_year + 1):
        age_primary = cal_year - primary.birth_year
        age_spouse = (cal_year - spouse.birth_year) if spouse else None

        # Determine phase
        age_for_phase = age_primary + (cal_year - inputs.current_year) / 1.0
        if cal_year < retirement_year:
            phase = ProjectionPhase.ACCUMULATION
        elif age_primary < 60:
            phase = ProjectionPhase.BRIDGE
        else:
            phase = ProjectionPhase.DISTRIBUTION

        balances_start = AccountBalances(
            hysa=balances.hysa,
            brokerage=balances.brokerage,
            roth_ira=balances.roth_ira,
            traditional_401k=balances.traditional_401k,
            roth_401k=balances.roth_401k,
        )

        tax_year = _get_tax_year(inputs.tax_years, cal_year)
        contribs = YearContributions()
        withdrawals = YearWithdrawals()
        ss_primary = 0.0
        ss_spouse = 0.0
        healthcare = 0.0
        roth_conversion = 0.0
        notes: list[str] = []
        is_depleted = False

        # ----------------------------------------------------------------
        # ACCUMULATION PHASE
        # ----------------------------------------------------------------
        if phase == ProjectionPhase.ACCUMULATION:
            limits = get_limits(
                age=age_primary,
                limit_rows=inputs.limit_rows,
                enable_catchup=inputs.contributions.enable_catchup,
                couple=True,
            )

            c = inputs.contributions
            trad_401k = min(c.traditional_401k_annual, limits.traditional_401k)
            roth_401k = min(c.roth_401k_annual, limits.roth_401k)
            roth_ira = min(c.roth_ira_annual, limits.roth_ira_couple_combined)
            employer = c.employer_match_annual

            contribs = YearContributions(
                traditional_401k=trad_401k,
                roth_401k=roth_401k,
                roth_ira=roth_ira,
                employer_match=employer,
                hysa=c.hysa_annual,
                brokerage=c.brokerage_annual,
            )

            # Apply contributions then grow
            balances = _apply_contributions(balances, contribs)
            balances = AccountBalances(
                hysa=_grow(balances.hysa, returns["hysa"]),
                brokerage=_grow(balances.brokerage, returns["brokerage"]),
                roth_ira=_grow(balances.roth_ira, returns["roth_ira"]),
                traditional_401k=_grow(
                    balances.traditional_401k, returns["traditional_401k"]
                ),
                roth_401k=_grow(balances.roth_401k, returns["roth_401k"]),
            )

        # ----------------------------------------------------------------
        # BRIDGE & DISTRIBUTION PHASES
        # ----------------------------------------------------------------
        else:
            retirement_year_offset = cal_year - retirement_year
            income_target = income_schedule[
                min(retirement_year_offset, len(income_schedule) - 1)
            ]

            # Healthcare cost (pre-Medicare bridge: ages 55–64)
            if age_primary < 65:
                healthcare_nominal = inflate(
                    inputs.healthcare_annual_cost,
                    inputs.inflation_rate,
                    cal_year - inputs.current_year,
                )
                healthcare = healthcare_nominal
                income_target += healthcare

            # Social Security
            if primary.ss_benefit and primary.ss_claim_start_year:
                ss_primary = annual_benefit_in_year(
                    benefit_estimate=primary.ss_benefit,
                    claim_start_year=primary.ss_claim_start_year,
                    projection_year=cal_year,
                    cola_rows=primary.ss_cola_rows,
                    assumed_cola=primary.assumed_future_ss_cola,
                )

            if spouse and spouse.ss_benefit and spouse.ss_claim_start_year:
                ss_spouse = annual_benefit_in_year(
                    benefit_estimate=spouse.ss_benefit,
                    claim_start_year=spouse.ss_claim_start_year,
                    projection_year=cal_year,
                    cola_rows=spouse.ss_cola_rows,
                    assumed_cola=spouse.assumed_future_ss_cola,
                )

            total_ss = ss_primary + ss_spouse
            total_ss_received += total_ss

            # Update ladder seasoning
            ladder_state = update_seasoning(ladder_state, cal_year, age_primary)
            roth_available = ladder_state.available_principal(cal_year, age_primary)

            # Net income needed from portfolio (after SS)
            portfolio_needed = max(0.0, income_target - total_ss)

            # ----------------------------------------------------------------
            # Withdrawal + Roth conversion: dynamic or static strategy
            # ----------------------------------------------------------------
            if inputs.withdrawal_strategy == "dynamic":
                (
                    balances,
                    withdrawals,
                    roth_conversion,
                    remaining_needed,
                    smart_notes,
                ) = _smart_withdraw_and_convert(
                    needed=portfolio_needed,
                    balances=balances,
                    ss_income=total_ss,
                    tax_year=tax_year,
                    ltcg_thresholds=inputs.ltcg_thresholds,
                    roth_available_principal=roth_available + balances.roth_ira
                    if age_primary >= 60
                    else roth_available,
                    enable_roth_ladder=inputs.enable_roth_ladder,
                    roth_ladder_target_bracket=inputs.roth_ladder_target_bracket,
                    roth_ladder_override=inputs.roth_ladder_overrides.get(cal_year),
                    traditional_balance=balances.traditional_401k,
                    ladder_state=ladder_state,
                    cal_year=cal_year,
                    age_primary=age_primary,
                    is_bridge=(phase == ProjectionPhase.BRIDGE),
                )
                notes.extend(n for n in smart_notes if n)
            else:
                # Legacy static path — preserved for regression testing
                if inputs.enable_roth_ladder and balances.traditional_401k > 0:
                    existing_income_estimate = total_ss * 0.85
                    from .roth_ladder import optimal_conversion_amount, execute_conversion, ConversionRecord

                    override_amount = inputs.roth_ladder_overrides.get(cal_year)
                    if override_amount is not None:
                        conv_amount = override_amount
                    else:
                        conv_amount = optimal_conversion_amount(
                            existing_income=existing_income_estimate,
                            traditional_balance=balances.traditional_401k,
                            tax_year=tax_year,
                            target_bracket_ceiling=inputs.roth_ladder_target_bracket,
                        )

                    if conv_amount > 0:
                        conv_result = execute_conversion(
                            year=cal_year,
                            age=age_primary,
                            existing_income=existing_income_estimate,
                            amount=conv_amount,
                            traditional_balance=balances.traditional_401k,
                            tax_year=tax_year,
                        )
                        roth_conversion = conv_result.conversion_amount
                        balances.traditional_401k -= roth_conversion
                        balances.roth_ira += roth_conversion
                        withdrawals.roth_conversion = roth_conversion
                        ladder_state.conversions.append(
                            ConversionRecord(
                                conversion_year=cal_year,
                                amount=roth_conversion,
                                tax_cost=conv_result.tax_cost,
                                available_year=conv_result.available_at_year,
                            )
                        )
                        notes.append(f"Roth conversion: ${roth_conversion:,.0f}")

                order = inputs.withdrawal_order or _default_withdrawal_order(
                    age_primary, phase == ProjectionPhase.BRIDGE
                )
                balances, withdrawals, remaining_needed = _withdraw_from_accounts(
                    needed=portfolio_needed,
                    balances=balances,
                    order=order,
                    roth_available_principal=roth_available + balances.roth_ira
                    if age_primary >= 60
                    else roth_available,
                )
                withdrawals.roth_conversion = roth_conversion

            if remaining_needed > 100:
                notes.append(f"Income shortfall: ${remaining_needed:,.0f}")
                is_depleted = True
                if depletion_year is None:
                    depletion_year = cal_year
                    depletion_age = age_primary

            # Compute taxes on actual withdrawals
            ordinary_income = withdrawals.traditional_401k + roth_conversion
            ltcg_income = withdrawals.brokerage  # All brokerage treated as LTCG gains

            tax_result = compute_total_tax(
                ordinary_income=ordinary_income,
                ltcg_income=ltcg_income,
                ss_benefits=total_ss,
                tax_year=tax_year,
                ltcg_thresholds=inputs.ltcg_thresholds,
            )
            total_tax_paid += tax_result.total_tax

            gross_income = (
                withdrawals.hysa
                + withdrawals.brokerage
                + withdrawals.roth_ira
                + withdrawals.roth_401k
                + withdrawals.traditional_401k
                + total_ss
            )
            net_income = gross_income - tax_result.total_tax

            # Grow remaining balances
            balances = AccountBalances(
                hysa=_grow(balances.hysa, returns["hysa"]),
                brokerage=_grow(balances.brokerage, returns["brokerage"]),
                roth_ira=_grow(balances.roth_ira, returns["roth_ira"]),
                traditional_401k=_grow(
                    balances.traditional_401k, returns["traditional_401k"]
                ),
                roth_401k=_grow(balances.roth_401k, returns["roth_401k"]),
            )

            projection_years.append(
                ProjectionYear(
                    calendar_year=cal_year,
                    age_primary=age_primary,
                    age_spouse=age_spouse,
                    phase=phase,
                    balances_start=balances_start,
                    balances_end=AccountBalances(
                        hysa=balances.hysa,
                        brokerage=balances.brokerage,
                        roth_ira=balances.roth_ira,
                        traditional_401k=balances.traditional_401k,
                        roth_401k=balances.roth_401k,
                    ),
                    contributions=contribs,
                    withdrawals=withdrawals,
                    ss_primary=ss_primary,
                    ss_spouse=ss_spouse,
                    healthcare_cost=healthcare,
                    tax_result=tax_result,
                    gross_income=gross_income,
                    net_income=net_income,
                    income_target=income_target,
                    income_gap=net_income - income_target,
                    roth_ladder_conversion=roth_conversion,
                    roth_available_principal=roth_available,
                    is_depleted=is_depleted,
                    notes=notes,
                )
            )
            continue

        # Accumulation year record
        projection_years.append(
            ProjectionYear(
                calendar_year=cal_year,
                age_primary=age_primary,
                age_spouse=age_spouse,
                phase=phase,
                balances_start=balances_start,
                balances_end=AccountBalances(
                    hysa=balances.hysa,
                    brokerage=balances.brokerage,
                    roth_ira=balances.roth_ira,
                    traditional_401k=balances.traditional_401k,
                    roth_401k=balances.roth_401k,
                ),
                contributions=contribs,
                withdrawals=withdrawals,
                notes=notes,
            )
        )

    final_balance = balances.total

    return ProjectionResult(
        scenario=scenario,
        years=projection_years,
        depletion_year=depletion_year,
        depletion_age=depletion_age,
        final_balance=final_balance,
        total_tax_paid=total_tax_paid,
        total_ss_received=total_ss_received,
        success=depletion_year is None,
    )