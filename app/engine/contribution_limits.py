# =============================================================================
# NestEgg - engine/contribution_limits.py
# IRS contribution limit enforcement and catch-up rules.
#
# Limit data is passed in from the DB (no direct DB access here).
# SECURE 2.0 enhanced catch-up for ages 60-63 is handled here.
# Backdoor Roth IRA: income limits are NOT enforced (user is assumed to be
# doing backdoor conversions; the engine just applies the base IRA limit).
# =============================================================================

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class LimitRow:
    """One row from the contribution_limits table."""

    account_type: str  # '401k' | 'roth_401k' | 'ira' | 'roth_ira'
    limit_type: str  # 'standard' | 'catchup'
    amount: float
    catchup_age: int | None


@dataclass
class AnnualLimits:
    """
    Resolved contribution limits for a specific person-year,
    incorporating age-based catch-up rules.
    """

    traditional_401k: float
    roth_401k: float
    ira_combined: float  # IRA + Roth IRA share one combined limit per person
    roth_ira_combined: float  # Same pool — kept separate for clarity in UI
    total_401k: float  # traditional + roth 401k (combined employee limit)

    # For the couple (NestEgg doubles the IRA limit since we treat both
    # Roth IRAs as one account; caller passes couple=True to get_limits())
    ira_couple_combined: float
    roth_ira_couple_combined: float


# SECURE 2.0 enhanced catch-up age window (ages 60–63 inclusive)
_SECURE2_CATCHUP_MIN_AGE = 60
_SECURE2_CATCHUP_MAX_AGE = 63
_SECURE2_401K_CATCHUP_ENHANCED = 11_250.00  # Total catch-up (not additional)
_STANDARD_CATCHUP_AGE = 50


def _get_standard(rows: list[LimitRow], account_type: str) -> float:
    for r in rows:
        if r.account_type == account_type and r.limit_type == "standard":
            return r.amount
    raise ValueError(f"No standard limit found for account_type={account_type!r}")


def _get_catchup(rows: list[LimitRow], account_type: str) -> float:
    for r in rows:
        if r.account_type == account_type and r.limit_type == "catchup":
            return r.amount
    return 0.0


def get_limits(
    age: int,
    limit_rows: list[LimitRow],
    enable_catchup: bool = True,
    couple: bool = True,
) -> AnnualLimits:
    """
    Resolve contribution limits for a given age and limit data from the DB.

    Args:
        age:            Person's age in the contribution year.
        limit_rows:     Rows from contribution_limits table for the relevant year.
        enable_catchup: Whether catch-up contributions are enabled (user toggle).
        couple:         If True, doubles the IRA/Roth IRA limit (two account holders).

    Returns:
        AnnualLimits dataclass with all resolved limits.
    """
    base_401k = _get_standard(limit_rows, "401k")
    catchup_401k = _get_catchup(limit_rows, "401k")
    base_ira = _get_standard(limit_rows, "ira")
    catchup_ira = _get_catchup(limit_rows, "ira")

    # --- 401(k) limit ---
    limit_401k = base_401k
    if enable_catchup and age >= _STANDARD_CATCHUP_AGE:
        if _SECURE2_CATCHUP_MIN_AGE <= age <= _SECURE2_CATCHUP_MAX_AGE:
            # SECURE 2.0: ages 60-63 get enhanced catch-up
            # The $11,250 replaces the standard $7,500 catch-up entirely
            limit_401k = base_401k + _SECURE2_401K_CATCHUP_ENHANCED
        else:
            limit_401k = base_401k + catchup_401k

    # --- IRA limit (per person) ---
    limit_ira_per_person = base_ira
    if enable_catchup and age >= _STANDARD_CATCHUP_AGE:
        limit_ira_per_person = base_ira + catchup_ira

    # --- Couple IRA (double for our combined Roth IRA account) ---
    ira_couple = limit_ira_per_person * (2 if couple else 1)

    return AnnualLimits(
        traditional_401k=limit_401k,
        roth_401k=limit_401k,  # Same employee limit as traditional
        ira_combined=limit_ira_per_person,
        roth_ira_combined=limit_ira_per_person,
        total_401k=limit_401k,  # Employee total (trad + roth share pool)
        ira_couple_combined=ira_couple,
        roth_ira_couple_combined=ira_couple,
    )


def clamp_contribution(
    desired: float,
    limit: float,
    label: str = "",
) -> tuple[float, bool]:
    """
    Clamp a desired contribution to the IRS limit.

    Returns:
        (clamped_amount, was_clamped)
    """
    if desired <= limit:
        return desired, False
    return limit, True


def total_employee_401k_within_limit(
    traditional_contrib: float,
    roth_contrib: float,
    limit: float,
) -> tuple[float, float]:
    """
    Ensure the combined traditional + Roth 401(k) employee contributions
    do not exceed the single employee limit.  Roth is filled first; any
    remainder goes to traditional.  Caller may reverse priority if desired.

    Returns:
        (adjusted_traditional, adjusted_roth)
    """
    roth_clamped = min(roth_contrib, limit)
    remaining = max(0.0, limit - roth_clamped)
    trad_clamped = min(traditional_contrib, remaining)
    return trad_clamped, roth_clamped


@dataclass
class ContributionWarning:
    account_type: str
    requested: float
    allowed: float
    message: str


def validate_contributions(
    age: int,
    trad_401k: float,
    roth_401k: float,
    roth_ira: float,
    limits: AnnualLimits,
) -> list[ContributionWarning]:
    """
    Validate a set of desired contributions against resolved limits.
    Returns a list of warnings (empty = all within limits).
    """
    warnings: list[ContributionWarning] = []

    combined_401k = trad_401k + roth_401k
    if combined_401k > limits.total_401k:
        warnings.append(
            ContributionWarning(
                account_type="401k_combined",
                requested=combined_401k,
                allowed=limits.total_401k,
                message=(
                    f"Combined 401(k) contributions ${combined_401k:,.0f} exceed "
                    f"the ${limits.total_401k:,.0f} limit for age {age}."
                ),
            )
        )

    if roth_ira > limits.roth_ira_couple_combined:
        warnings.append(
            ContributionWarning(
                account_type="roth_ira",
                requested=roth_ira,
                allowed=limits.roth_ira_couple_combined,
                message=(
                    f"Roth IRA contributions ${roth_ira:,.0f} exceed the combined "
                    f"couple limit of ${limits.roth_ira_couple_combined:,.0f}."
                ),
            )
        )

    return warnings
