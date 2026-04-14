# =============================================================================
# NestEgg - engine/contribution_planner.py
# Contribution optimizer: finds the minimum annual contributions across all
# enabled account types that allow the portfolio to survive to plan_to_age.
#
# Strategy:
#   1. Binary-search on a single "total annual savings" scalar S.
#   2. At each S, allocate across enabled accounts in a tax-efficient
#      waterfall order, respecting IRS limits per account.
#   3. Run the full projection to test survival.
#   4. Narrow the bracket until the minimum S that achieves survival is found.
#
# Waterfall allocation order (most tax-advantaged first):
#   traditional_401k → roth_401k → roth_ira → hysa → brokerage
#
# Traditional 401k is filled first because pre-tax contributions reduce
# current taxable income, which is the highest-leverage lever during the
# accumulation phase for high earners. Roth accounts follow because
# tax-free growth compounds over the long accumulation window. HYSA and
# brokerage are filled last as the "overflow" buckets with no tax shelter.
#
# The solver returns the per-account annual amounts at the found optimum,
# plus a full projection so the UI can show the resulting balance trajectory.
#
# All inputs are passed in — no DB access.
# =============================================================================

from __future__ import annotations

from dataclasses import dataclass, field

from .contribution_limits import AnnualLimits, LimitRow, get_limits
from .projection import (
    ContributionInputs,
    ProjectionInputs,
    ProjectionResult,
    ReturnScenario,
    run_projection,
)


# ---------------------------------------------------------------------------
# Input / output structures
# ---------------------------------------------------------------------------


@dataclass
class AccountInclusion:
    """Which accounts the user wants the solver to contribute to."""
    traditional_401k: bool = True
    roth_401k: bool = True
    roth_ira: bool = True
    hysa: bool = True
    brokerage: bool = True


@dataclass
class ContributionPlanResult:
    """
    Result of the contribution planner solver.

    `solved` is True if a feasible contribution level was found that lets
    the portfolio survive to plan_to_age.  If False, even contributing the
    maximum IRS-legal amount to every enabled account is not enough —
    the UI should surface this as a warning rather than a hard error.
    """
    solved: bool

    # Per-account recommended annual contributions
    traditional_401k_annual: float = 0.0
    roth_401k_annual: float = 0.0
    roth_ira_annual: float = 0.0
    hysa_annual: float = 0.0
    brokerage_annual: float = 0.0
    employer_match_annual: float = 0.0  # Passed through unchanged

    total_annual_contribution: float = 0.0

    # IRS limits that applied (for UI display)
    limit_traditional_401k: float = 0.0
    limit_roth_401k: float = 0.0
    limit_roth_ira: float = 0.0

    # The resulting projection at the solved contribution level
    projection: ProjectionResult | None = None

    # Informational
    iterations: int = 0
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Allocation waterfall
# ---------------------------------------------------------------------------


def _allocate(
    total: float,
    inclusion: AccountInclusion,
    limits: AnnualLimits,
    employer_match: float,
) -> tuple[float, float, float, float, float]:
    """
    Allocate `total` dollars across enabled accounts in waterfall order,
    respecting IRS limits.

    Returns:
        (trad_401k, roth_401k, roth_ira, hysa, brokerage)
    """
    remaining = total
    trad = roth_401k = roth_ira = hysa = brokerage = 0.0

    # 1. Traditional 401k — pre-tax, highest near-term tax leverage
    if inclusion.traditional_401k and remaining > 0:
        # Combined 401k employee limit is shared between trad and roth 401k.
        # We fill trad first up to the full limit; roth 401k gets the remainder.
        take = min(remaining, limits.traditional_401k)
        trad = take
        remaining -= take

    # 2. Roth 401k — after-tax, but same employee limit pool as trad
    if inclusion.roth_401k and remaining > 0:
        # Whatever trad hasn't used of the combined 401k cap is available
        combined_401k_used = trad
        roth_401k_room = max(0.0, limits.roth_401k - combined_401k_used)
        take = min(remaining, roth_401k_room)
        roth_401k = take
        remaining -= take

    # 3. Roth IRA — tax-free growth, couple combined limit
    if inclusion.roth_ira and remaining > 0:
        take = min(remaining, limits.roth_ira_couple_combined)
        roth_ira = take
        remaining -= take

    # 4. HYSA — liquid emergency/bridge buffer, no IRS cap
    if inclusion.hysa and remaining > 0:
        hysa = remaining
        remaining = 0.0

    # 5. Brokerage — taxable overflow, no IRS cap
    #    Only reached if HYSA is excluded
    if inclusion.brokerage and remaining > 0:
        brokerage = remaining
        remaining = 0.0

    return trad, roth_401k, roth_ira, hysa, brokerage


def _max_irs_contribution(
    inclusion: AccountInclusion,
    limits: AnnualLimits,
) -> float:
    """
    The maximum IRS-legal contribution to all enabled IRS-capped accounts.
    HYSA and brokerage are uncapped — for feasibility checking we cap them
    at a generous $500k (effectively unlimited for planning purposes).
    """
    UNCAPPED = 500_000.0
    total = 0.0
    if inclusion.traditional_401k:
        total += limits.traditional_401k
    if inclusion.roth_401k:
        # Roth 401k shares the same pool as trad — only add if trad not included
        if not inclusion.traditional_401k:
            total += limits.roth_401k
    if inclusion.roth_ira:
        total += limits.roth_ira_couple_combined
    if inclusion.hysa:
        total += UNCAPPED
    if inclusion.brokerage:
        total += UNCAPPED
    return total


# ---------------------------------------------------------------------------
# Solver
# ---------------------------------------------------------------------------


def solve_contributions(
    base_inputs: ProjectionInputs,
    inclusion: AccountInclusion,
    scenario: ReturnScenario = ReturnScenario.BASE,
    tolerance: float = 500.0,
    max_iterations: int = 40,
) -> ContributionPlanResult:
    """
    Binary-search for the minimum total annual contribution that lets the
    portfolio survive to plan_to_age.

    Args:
        base_inputs:    Full ProjectionInputs with current balances, SS,
                        tax data, etc.  Contributions will be overridden
                        by the solver.
        inclusion:      Which accounts to include in the solve.
        scenario:       Return scenario to test against.
        tolerance:      Stop when high - low < tolerance (dollars).
        max_iterations: Safety cap on iterations.

    Returns:
        ContributionPlanResult with recommended per-account amounts and
        the resulting projection.
    """
    # Resolve IRS limits using the primary person's current age
    from ..utils import current_year as _current_year
    primary_age = _current_year() - base_inputs.primary.birth_year
    limits = get_limits(
        age=primary_age,
        limit_rows=base_inputs.limit_rows,
        enable_catchup=base_inputs.contributions.enable_catchup,
        couple=True,
    )

    employer_match = base_inputs.contributions.employer_match_annual

    def _test(total_contrib: float) -> tuple[bool, ProjectionResult]:
        trad, r401k, rira, h, b = _allocate(
            total_contrib, inclusion, limits, employer_match
        )
        contribs = ContributionInputs(
            traditional_401k_annual=trad,
            roth_401k_annual=r401k,
            roth_ira_annual=rira,
            hysa_annual=h,
            brokerage_annual=b,
            employer_match_annual=employer_match,
            enable_catchup=base_inputs.contributions.enable_catchup,
        )
        test_inputs = ProjectionInputs(
            current_year=base_inputs.current_year,
            primary=base_inputs.primary,
            spouse=base_inputs.spouse,
            accounts=base_inputs.accounts,
            contributions=contribs,
            limit_rows=base_inputs.limit_rows,
            desired_retirement_income_today=base_inputs.desired_retirement_income_today,
            current_income=base_inputs.current_income,
            inflation_rate=base_inputs.inflation_rate,
            plan_to_age=base_inputs.plan_to_age,
            healthcare_annual_cost=base_inputs.healthcare_annual_cost,
            tax_years=base_inputs.tax_years,
            ltcg_thresholds=base_inputs.ltcg_thresholds,
            enable_roth_ladder=base_inputs.enable_roth_ladder,
            roth_ladder_target_bracket=base_inputs.roth_ladder_target_bracket,
            roth_ladder_overrides=base_inputs.roth_ladder_overrides,
            withdrawal_order=base_inputs.withdrawal_order,
        )
        result = run_projection(test_inputs, scenario)
        return result.success, result

    # --- Feasibility check: can we survive at all with max contributions? ---
    max_contrib = _max_irs_contribution(inclusion, limits)
    feasible, max_result = _test(max_contrib)

    if not feasible:
        # Even maxing out every account isn't enough.
        # Return the max-contribution result so the UI can still show
        # how close we get — but flag as unsolved.
        trad, r401k, rira, h, b = _allocate(max_contrib, inclusion, limits, employer_match)
        notes = [
            "Even at maximum contributions across all enabled accounts, the portfolio "
            "does not survive to the target age. Consider increasing current balances, "
            "reducing retirement income goals, or extending the working years."
        ]
        return ContributionPlanResult(
            solved=False,
            traditional_401k_annual=trad,
            roth_401k_annual=r401k,
            roth_ira_annual=rira,
            hysa_annual=h,
            brokerage_annual=b,
            employer_match_annual=employer_match,
            total_annual_contribution=max_contrib,
            limit_traditional_401k=limits.traditional_401k,
            limit_roth_401k=limits.roth_401k,
            limit_roth_ira=limits.roth_ira_couple_combined,
            projection=max_result,
            iterations=1,
            notes=notes,
        )

    # --- Check zero: maybe no contributions are needed at all ---
    zero_ok, zero_result = _test(0.0)
    if zero_ok:
        return ContributionPlanResult(
            solved=True,
            traditional_401k_annual=0.0,
            roth_401k_annual=0.0,
            roth_ira_annual=0.0,
            hysa_annual=0.0,
            brokerage_annual=0.0,
            employer_match_annual=employer_match,
            total_annual_contribution=0.0,
            limit_traditional_401k=limits.traditional_401k,
            limit_roth_401k=limits.roth_401k,
            limit_roth_ira=limits.roth_ira_couple_combined,
            projection=zero_result,
            iterations=1,
            notes=["Current balances are sufficient to reach the target without additional contributions."],
        )

    # --- Binary search ---
    low = 0.0
    high = max_contrib
    best_result = max_result
    best_total = max_contrib
    iterations = 0

    while high - low > tolerance and iterations < max_iterations:
        mid = (low + high) / 2.0
        success, result = _test(mid)
        iterations += 1

        if success:
            best_result = result
            best_total = mid
            high = mid
        else:
            low = mid

    # Resolve final allocation at best_total
    trad, r401k, rira, h, b = _allocate(best_total, inclusion, limits, employer_match)

    notes: list[str] = []
    if trad >= limits.traditional_401k and inclusion.traditional_401k:
        notes.append(f"Traditional 401(k) is at the IRS limit (${limits.traditional_401k:,.0f}/yr).")
    if r401k >= limits.roth_401k and inclusion.roth_401k and not inclusion.traditional_401k:
        notes.append(f"Roth 401(k) is at the IRS limit (${limits.roth_401k:,.0f}/yr).")
    if rira >= limits.roth_ira_couple_combined and inclusion.roth_ira:
        notes.append(f"Roth IRA is at the combined couple limit (${limits.roth_ira_couple_combined:,.0f}/yr).")
    if h > 0 and inclusion.hysa:
        notes.append("HYSA contributions are filling remaining savings beyond tax-advantaged caps.")
    if b > 0 and inclusion.brokerage:
        notes.append("Brokerage contributions are filling remaining savings beyond tax-advantaged caps.")

    return ContributionPlanResult(
        solved=True,
        traditional_401k_annual=round(trad, 2),
        roth_401k_annual=round(r401k, 2),
        roth_ira_annual=round(rira, 2),
        hysa_annual=round(h, 2),
        brokerage_annual=round(b, 2),
        employer_match_annual=employer_match,
        total_annual_contribution=round(best_total, 2),
        limit_traditional_401k=limits.traditional_401k,
        limit_roth_401k=limits.roth_401k,
        limit_roth_ira=limits.roth_ira_couple_combined,
        projection=best_result,
        iterations=iterations,
        notes=notes,
    )
