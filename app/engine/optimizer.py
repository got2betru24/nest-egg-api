# =============================================================================
# NestEgg - engine/optimizer.py
# Retirement strategy optimizer.
#
# Objective: Maximize portfolio longevity to plan_to_age by minimizing
# lifetime tax burden through optimal:
#   1. Withdrawal order (which accounts to draw from each year)
#   2. Roth conversion ladder (how much to convert, at what bracket)
#   3. Social Security claiming age (for primary and spouse independently)
#   4. Retirement age sensitivity (how does longevity change by retire age)
#
# Approach:
#   - Run the projection engine with multiple configurations.
#   - Score each configuration by portfolio survival and residual balance.
#   - Return the best configuration with full rationale.
#
# This is a deterministic optimizer (not stochastic). It searches a
# constrained parameter space rather than gradient descent or Monte Carlo.
#
# All functions are pure — no DB access.
# =============================================================================

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product

from .projection import (
    ProjectionInputs,
    ProjectionResult,
    ReturnScenario,
    run_projection,
)
from .social_security import (
    BendPointRow,
    FRARule,
    AWIRow,
    EarningsRecord,
    SSBenefitEstimate,
    estimate_benefit,
    get_fra,
)
from .tax_engine import TaxYear, bracket_fill_at


# ---------------------------------------------------------------------------
# Optimizer configuration
# ---------------------------------------------------------------------------

@dataclass
class SSClaimingOption:
    """One Social Security claiming age option to evaluate."""
    claim_age_years: int
    claim_age_months: int
    label: str  # e.g. "Early (62)", "FRA (67)", "Late (70)"


@dataclass
class OptimizerConfig:
    """
    Configuration space for the optimizer.
    Defines what parameter combinations to search.
    """
    # SS claiming ages to test for primary
    primary_ss_options: list[SSClaimingOption] = field(default_factory=lambda: [
        SSClaimingOption(62, 0, "Early (62)"),
        SSClaimingOption(67, 0, "FRA (67)"),
        SSClaimingOption(70, 0, "Late (70)"),
    ])

    # SS claiming ages to test for spouse (if applicable)
    spouse_ss_options: list[SSClaimingOption] = field(default_factory=lambda: [
        SSClaimingOption(62, 0, "Early (62)"),
        SSClaimingOption(67, 0, "FRA (67)"),
        SSClaimingOption(70, 0, "Late (70)"),
    ])

    # Roth ladder bracket ceilings to test
    roth_ladder_ceilings: list[float] = field(default_factory=lambda: [0.12, 0.22, 0.24])

    # Whether to test with/without Roth ladder
    test_roth_ladder: bool = True
    test_no_roth_ladder: bool = False

    # Return scenario to optimize against (base is most useful)
    optimize_against_scenario: ReturnScenario = ReturnScenario.BASE


# ---------------------------------------------------------------------------
# Optimizer result
# ---------------------------------------------------------------------------

@dataclass
class OptimizedStrategy:
    """
    The recommended strategy and its rationale.
    """
    # SS claiming recommendations
    primary_ss_claim_age_years: int
    primary_ss_claim_age_months: int
    primary_ss_claim_label: str
    spouse_ss_claim_age_years: int | None
    spouse_ss_claim_age_months: int | None
    spouse_ss_claim_label: str | None

    # Roth ladder recommendation
    roth_ladder_enabled: bool
    roth_ladder_target_bracket: float

    # Projection result for the recommended configuration
    projection: ProjectionResult

    # Scoring
    score: float                        # Higher = better
    portfolio_survives: bool
    residual_balance: float
    total_tax_saved_vs_no_ladder: float | None

    # Comparison data
    all_results: list["OptimizationCandidate"] = field(default_factory=list)
    rationale: list[str] = field(default_factory=list)


@dataclass
class OptimizationCandidate:
    """One evaluated configuration in the optimizer search."""
    primary_ss_label: str
    spouse_ss_label: str | None
    roth_ladder_enabled: bool
    roth_ladder_ceiling: float
    projection: ProjectionResult
    score: float
    survives: bool


# ---------------------------------------------------------------------------
# Scoring function
# ---------------------------------------------------------------------------

def score_projection(result: ProjectionResult, plan_to_age: int) -> float:
    """
    Score a projection result. Higher = better.

    Scoring factors:
        +1,000,000 if portfolio survives to plan_to_age
        + final_balance (residual wealth)
        - total_tax_paid (minimize taxes)
        - 500,000 penalty per year short of plan_to_age (if depleted early)
    """
    base = 0.0

    if result.success:
        base += 1_000_000.0
        base += result.final_balance
    else:
        if result.depletion_year and result.years:
            last_year = result.years[-1].calendar_year
            depletion_year = result.depletion_year
            years_short = last_year - depletion_year
            base -= years_short * 500_000.0

    # Tax efficiency bonus (lower taxes = higher score)
    base -= result.total_tax_paid * 0.5

    return base


# ---------------------------------------------------------------------------
# SS claiming sensitivity
# ---------------------------------------------------------------------------

def breakeven_analysis(
    early_annual: float,
    late_annual: float,
    early_start_age: int,
    late_start_age: int,
) -> float:
    """
    Calculate the break-even age between two SS claiming strategies.

    Args:
        early_annual:     Annual benefit if claiming early.
        late_annual:      Annual benefit if claiming late.
        early_start_age:  Age at which early benefits begin.
        late_start_age:   Age at which late benefits begin.

    Returns:
        Break-even age (float). If early > late (unusual), returns infinity.
    """
    if late_annual <= early_annual:
        return float("inf")

    # Years of early benefits before late strategy begins
    years_early_exclusive = late_start_age - early_start_age
    early_total_at_late_start = early_annual * years_early_exclusive

    # Annual advantage of late strategy
    annual_delta = late_annual - early_annual

    # Breakeven: early cumulative = late cumulative
    # early_total_at_late_start + early_annual * x = late_annual * x
    # early_total_at_late_start = (late_annual - early_annual) * x
    years_to_breakeven = early_total_at_late_start / annual_delta

    return late_start_age + years_to_breakeven


# ---------------------------------------------------------------------------
# Roth ladder optimizer
# ---------------------------------------------------------------------------

def optimal_roth_ladder_ceiling(
    base_inputs: ProjectionInputs,
    ceilings_to_test: list[float] = None,
) -> tuple[float, float]:
    """
    Find the Roth ladder bracket ceiling that maximizes portfolio longevity.

    Args:
        base_inputs:      Projection inputs (SS already set).
        ceilings_to_test: List of bracket rates to try.

    Returns:
        (best_ceiling, best_score)
    """
    if ceilings_to_test is None:
        ceilings_to_test = [0.10, 0.12, 0.22, 0.24]

    best_ceiling = 0.22
    best_score = float("-inf")

    for ceiling in ceilings_to_test:
        test_inputs = ProjectionInputs(
            **{
                k: v for k, v in base_inputs.__dict__.items()
                if k not in ("roth_ladder_target_bracket",)
            }
        )
        test_inputs.roth_ladder_target_bracket = ceiling
        test_inputs.enable_roth_ladder = True

        result = run_projection(test_inputs, base_inputs.contributions.__class__)
        s = score_projection(result, base_inputs.plan_to_age)
        if s > best_score:
            best_score = s
            best_ceiling = ceiling

    return best_ceiling, best_score


# ---------------------------------------------------------------------------
# Main optimizer
# ---------------------------------------------------------------------------

def run_optimizer(
    base_inputs: ProjectionInputs,
    primary_earnings: list[EarningsRecord],
    primary_birth_year: int,
    spouse_birth_year: int | None,
    spouse_earnings: list[EarningsRecord] | None,
    awi_rows: list[AWIRow],
    bend_point_row: BendPointRow,
    fra_rules: list[FRARule],
    ss_cola_rows: list[dict],
    config: OptimizerConfig | None = None,
    spouse_use_spousal_benefit: bool = True,
    spousal_pct: float = 0.50,
) -> OptimizedStrategy:
    """
    Run the full optimizer across the configured parameter space.

    For each combination of (primary SS age, spouse SS age, roth ladder ceiling):
        1. Build SS benefit estimates
        2. Run projection
        3. Score result

    Returns the highest-scoring strategy with full rationale.

    Args:
        base_inputs:             Base projection inputs (SS fields will be overridden).
        primary_earnings:        Primary's SS earnings history.
        primary_birth_year:      Primary's birth year.
        spouse_birth_year:       Spouse's birth year (None if no spouse).
        spouse_earnings:         Spouse's earnings (None if using spousal benefit).
        awi_rows:                AWI table.
        bend_point_row:          Bend points for primary's benefit year.
        fra_rules:               FRA table.
        ss_cola_rows:            COLA history.
        config:                  Optimizer config (uses defaults if None).
        spouse_use_spousal_benefit: If True, spouse takes 50% of primary PIA.
        spousal_pct:             Fraction of primary PIA for spousal benefit.
    """
    if config is None:
        config = OptimizerConfig()

    candidates: list[OptimizationCandidate] = []
    best_score = float("-inf")
    best_candidate: OptimizationCandidate | None = None

    # Pre-compute primary SS estimates for each claiming option
    primary_estimates: dict[str, SSBenefitEstimate] = {}
    for opt in config.primary_ss_options:
        est = estimate_benefit(
            birth_year=primary_birth_year,
            claim_age_years=opt.claim_age_years,
            claim_age_months=opt.claim_age_months,
            earnings_records=primary_earnings,
            awi_rows=awi_rows,
            bend_point_row=bend_point_row,
            fra_rules=fra_rules,
            assumed_future_income=base_inputs.current_income,
            current_age=base_inputs.current_year - primary_birth_year,
            retirement_age=base_inputs.primary.retirement_age,
        )
        primary_estimates[opt.label] = est

    # Pre-compute spouse SS estimates
    spouse_estimates: dict[str, SSBenefitEstimate] = {}
    if spouse_birth_year and config.spouse_ss_options:
        for opt in config.spouse_ss_options:
            if spouse_use_spousal_benefit:
                # Spousal benefit: use primary PIA at FRA claiming age for reference
                fra_est = primary_estimates.get("FRA (67)") or list(primary_estimates.values())[0]
                from .social_security import compute_spousal_benefit
                spousal = compute_spousal_benefit(
                    primary_pia=fra_est.pia,
                    spouse_claim_age_years=opt.claim_age_years,
                    spouse_claim_age_months=opt.claim_age_months,
                    spouse_birth_year=spouse_birth_year,
                    fra_rules=fra_rules,
                    spousal_pct=spousal_pct,
                )
                # Wrap in SSBenefitEstimate for projection engine
                spouse_fra_y, spouse_fra_m = get_fra(spouse_birth_year, fra_rules)
                from .social_security import claiming_adjustment_factor
                adj, months = claiming_adjustment_factor(
                    opt.claim_age_years, opt.claim_age_months,
                    spouse_fra_y, spouse_fra_m,
                )
                est = SSBenefitEstimate(
                    person_birth_year=spouse_birth_year,
                    claim_age_years=opt.claim_age_years,
                    claim_age_months=opt.claim_age_months,
                    fra_years=spouse_fra_y,
                    fra_months=spouse_fra_m,
                    aime=0.0,
                    pia=fra_est.pia * spousal_pct,
                    monthly_benefit=spousal.spousal_monthly,
                    annual_benefit=spousal.spousal_annual,
                    adjustment_factor=adj,
                    is_early=months < 0,
                    is_late=months > 0,
                    months_from_fra=months,
                )
            else:
                est = estimate_benefit(
                    birth_year=spouse_birth_year,
                    claim_age_years=opt.claim_age_years,
                    claim_age_months=opt.claim_age_months,
                    earnings_records=spouse_earnings or [],
                    awi_rows=awi_rows,
                    bend_point_row=bend_point_row,
                    fra_rules=fra_rules,
                )
            spouse_estimates[opt.label] = est

    # Roth ladder options
    ladder_options: list[tuple[bool, float]] = []
    if config.test_roth_ladder:
        for ceiling in config.roth_ladder_ceilings:
            ladder_options.append((True, ceiling))
    if config.test_no_roth_ladder:
        ladder_options.append((False, 0.22))

    # Search space: primary SS x spouse SS x roth ladder
    spouse_options = config.spouse_ss_options if spouse_birth_year else [None]

    for p_opt, s_opt_maybe, (ladder_on, ceiling) in product(
        config.primary_ss_options, spouse_options, ladder_options
    ):
        # Build projection inputs for this configuration
        primary_est = primary_estimates[p_opt.label]
        primary_claim_year = primary_birth_year + p_opt.claim_age_years

        from .projection import PersonInputs
        p_inputs = PersonInputs(
            birth_year=primary_birth_year,
            retirement_age=base_inputs.primary.retirement_age,
            ss_benefit=primary_est,
            ss_claim_start_year=primary_claim_year,
            ss_cola_rows=ss_cola_rows,
            assumed_future_ss_cola=base_inputs.primary.assumed_future_ss_cola,
        )

        s_inputs = None
        s_label = None
        s_claim_y = None
        s_claim_m = None
        if s_opt_maybe and spouse_birth_year:
            s_est = spouse_estimates[s_opt_maybe.label]
            spouse_claim_year = spouse_birth_year + s_opt_maybe.claim_age_years
            from .projection import PersonInputs as PI
            s_inputs = PI(
                birth_year=spouse_birth_year,
                retirement_age=base_inputs.spouse.retirement_age if base_inputs.spouse else 55,
                ss_benefit=s_est,
                ss_claim_start_year=spouse_claim_year,
                ss_cola_rows=ss_cola_rows,
                assumed_future_ss_cola=0.025,
            )
            s_label = s_opt_maybe.label
            s_claim_y = s_opt_maybe.claim_age_years
            s_claim_m = s_opt_maybe.claim_age_months

        # Build test inputs
        import dataclasses
        test_inputs = dataclasses.replace(
            base_inputs,
            primary=p_inputs,
            spouse=s_inputs,
            enable_roth_ladder=ladder_on,
            roth_ladder_target_bracket=ceiling,
        )

        result = run_projection(test_inputs, config.optimize_against_scenario)
        s = score_projection(result, base_inputs.plan_to_age)

        candidate = OptimizationCandidate(
            primary_ss_label=p_opt.label,
            spouse_ss_label=s_label,
            roth_ladder_enabled=ladder_on,
            roth_ladder_ceiling=ceiling,
            projection=result,
            score=s,
            survives=result.success,
        )
        candidates.append(candidate)

        if s > best_score:
            best_score = s
            best_candidate = candidate

    if best_candidate is None:
        raise RuntimeError("Optimizer found no valid candidates.")

    # Build rationale
    rationale: list[str] = []
    bc = best_candidate

    rationale.append(
        f"Recommended SS claiming age: {bc.primary_ss_label} for primary."
    )
    if bc.spouse_ss_label:
        rationale.append(f"Spouse SS claiming: {bc.spouse_ss_label}.")

    if bc.roth_ladder_enabled:
        rationale.append(
            f"Roth conversion ladder enabled, converting up to the "
            f"{bc.roth_ladder_ceiling:.0%} bracket each year."
        )
    else:
        rationale.append("No Roth conversion ladder (not beneficial for this scenario).")

    if bc.survives:
        rationale.append(
            f"Portfolio survives to plan age with ${bc.projection.final_balance:,.0f} remaining."
        )
    else:
        depletion = bc.projection.depletion_age
        rationale.append(
            f"Warning: Portfolio depletes at age {depletion}. "
            f"Consider reducing spending or delaying retirement."
        )

    # Tax comparison vs no-ladder
    no_ladder_candidates = [c for c in candidates if not c.roth_ladder_enabled]
    tax_saved: float | None = None
    if no_ladder_candidates and bc.roth_ladder_enabled:
        best_no_ladder = max(no_ladder_candidates, key=lambda c: c.score)
        tax_saved = (
            best_no_ladder.projection.total_tax_paid
            - bc.projection.total_tax_paid
        )
        if tax_saved > 0:
            rationale.append(
                f"Roth ladder saves an estimated ${tax_saved:,.0f} in lifetime taxes."
            )

    return OptimizedStrategy(
        primary_ss_claim_age_years=config.primary_ss_options[
            [o.label for o in config.primary_ss_options].index(bc.primary_ss_label)
        ].claim_age_years,
        primary_ss_claim_age_months=config.primary_ss_options[
            [o.label for o in config.primary_ss_options].index(bc.primary_ss_label)
        ].claim_age_months,
        primary_ss_claim_label=bc.primary_ss_label,
        spouse_ss_claim_age_years=bc.spouse_ss_label and config.spouse_ss_options[
            [o.label for o in config.spouse_ss_options].index(bc.spouse_ss_label)
        ].claim_age_years if bc.spouse_ss_label and config.spouse_ss_options else None,
        spouse_ss_claim_age_months=bc.spouse_ss_label and config.spouse_ss_options[
            [o.label for o in config.spouse_ss_options].index(bc.spouse_ss_label)
        ].claim_age_months if bc.spouse_ss_label and config.spouse_ss_options else None,
        spouse_ss_claim_label=bc.spouse_ss_label,
        roth_ladder_enabled=bc.roth_ladder_enabled,
        roth_ladder_target_bracket=bc.roth_ladder_ceiling,
        projection=bc.projection,
        score=bc.score,
        portfolio_survives=bc.survives,
        residual_balance=bc.projection.final_balance,
        total_tax_saved_vs_no_ladder=tax_saved,
        all_results=candidates,
        rationale=rationale,
    )
