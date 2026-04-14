# =============================================================================
# NestEgg - app/models.py
# Pydantic v2 request/response models and enums for the FastAPI layer.
# Engine dataclasses are separate (app/engine/). These models handle
# API serialization, validation, and documentation.
# =============================================================================

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class FilingStatus(str, Enum):
    MFJ = "married_filing_jointly"
    SINGLE = "single"


class AccountType(str, Enum):
    HYSA = "hysa"
    BROKERAGE = "brokerage"
    ROTH_IRA = "roth_ira"
    TRADITIONAL_401K = "traditional_401k"
    ROTH_401K = "roth_401k"


class ReturnScenario(str, Enum):
    CONSERVATIVE = "conservative"
    BASE = "base"
    OPTIMISTIC = "optimistic"


class SolveMode(str, Enum):
    FIXED = "fixed"
    SOLVE_FOR = "solve_for"


class BridgeMethodEnum(str, Enum):
    RULE_OF_55 = "rule_of_55"
    SEPP_FIXED_AMORTIZATION = "sepp_fixed_amortization"
    SEPP_RMD = "sepp_rmd"
    TAXABLE_ONLY = "taxable_only"


# ---------------------------------------------------------------------------
# Scenario CRUD
# ---------------------------------------------------------------------------


class ScenarioBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: str | None = None


class ScenarioCreate(ScenarioBase):
    pass


class ScenarioUpdate(ScenarioBase):
    name: str | None = Field(None, min_length=1, max_length=120)


class ScenarioOut(ScenarioBase):
    id: int
    created_at: str
    updated_at: str
    is_active: bool

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Person
# ---------------------------------------------------------------------------


class PersonBase(BaseModel):
    role: str = Field(..., pattern="^(primary|spouse)$")
    birth_year: int = Field(..., ge=1940, le=2005)
    birth_month: int = Field(1, ge=1, le=12)
    planned_retirement_age: int = Field(55, ge=50, le=70)


class PersonCreate(PersonBase):
    pass


class PersonOut(PersonBase):
    id: int
    scenario_id: int

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Scenario Assumptions
# ---------------------------------------------------------------------------


class AssumptionsBase(BaseModel):
    inflation_rate: float = Field(0.03, ge=0.0, le=0.20)
    plan_to_age: int = Field(90, ge=70, le=105)
    filing_status: FilingStatus = FilingStatus.MFJ
    current_income: float = Field(0.0, ge=0.0)
    desired_retirement_income: float = Field(
        0.0, ge=0.0, description="In today's dollars"
    )
    healthcare_annual_cost: float = Field(
        0.0, ge=0.0, description="Annual pre-Medicare healthcare cost (today's dollars)"
    )
    enable_catchup_contributions: bool = False
    enable_roth_ladder: bool = False
    return_scenario: ReturnScenario = ReturnScenario.BASE


class AssumptionsCreate(AssumptionsBase):
    pass


class AssumptionsOut(AssumptionsBase):
    id: int
    scenario_id: int

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


class AccountReturnAssumptions(BaseModel):
    return_conservative: float = Field(0.04, ge=0.0, le=0.30)
    return_base: float = Field(0.07, ge=0.0, le=0.30)
    return_optimistic: float = Field(0.10, ge=0.0, le=0.30)

    @model_validator(mode="after")
    def check_ordering(self) -> "AccountReturnAssumptions":
        if not (self.return_conservative <= self.return_base <= self.return_optimistic):
            raise ValueError("Returns must satisfy: conservative <= base <= optimistic")
        return self


class AccountBase(BaseModel):
    account_type: AccountType
    label: str | None = None
    current_balance: float = Field(0.0, ge=0.0)
    return_conservative: float = Field(0.04, ge=0.0, le=0.30)
    return_base: float = Field(0.07, ge=0.0, le=0.30)
    return_optimistic: float = Field(0.10, ge=0.0, le=0.30)


class AccountCreate(AccountBase):
    pass


class AccountOut(AccountBase):
    id: int
    scenario_id: int

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Contributions
# ---------------------------------------------------------------------------


class ContributionBase(BaseModel):
    annual_amount: float = Field(0.0, ge=0.0)
    employer_match_amount: float = Field(0.0, ge=0.0)
    enforce_irs_limits: bool = True
    solve_mode: SolveMode = SolveMode.FIXED


class ContributionCreate(ContributionBase):
    pass


class ContributionOut(ContributionBase):
    id: int
    account_id: int

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Social Security
# ---------------------------------------------------------------------------


class SSEarningsRow(BaseModel):
    year: int = Field(..., ge=1950, le=2040)
    earnings: float = Field(..., ge=0.0)


class SSEarningsUpload(BaseModel):
    person_id: int
    earnings: list[SSEarningsRow]


class SSClaimingBase(BaseModel):
    claim_age_years: int = Field(67, ge=62, le=70)
    claim_age_months: int = Field(0, ge=0, le=11)
    use_spousal_benefit: bool = False
    spousal_benefit_pct: float = Field(0.50, ge=0.0, le=0.50)


class SSClaimingCreate(SSClaimingBase):
    pass


class SSClaimingOut(SSClaimingBase):
    id: int
    person_id: int

    model_config = {"from_attributes": True}


class SSBenefitEstimateOut(BaseModel):
    claim_age_years: int
    claim_age_months: int
    fra_years: int
    fra_months: int
    aime: float
    pia: float
    monthly_benefit: float
    annual_benefit: float
    adjustment_factor: float
    is_early: bool
    is_late: bool
    months_from_fra: int


class SSClaimingComparisonOut(BaseModel):
    early: SSBenefitEstimateOut
    fra: SSBenefitEstimateOut
    late: SSBenefitEstimateOut


# ---------------------------------------------------------------------------
# Roth Conversions
# ---------------------------------------------------------------------------


class RothConversionOverride(BaseModel):
    plan_year: int = Field(..., ge=2024, le=2080)
    amount: float = Field(..., ge=0.0)


class RothConversionOut(RothConversionOverride):
    id: int
    scenario_id: int
    source_account: str
    is_optimizer_suggested: bool

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Projection request/response
# ---------------------------------------------------------------------------


class ProjectionRequest(BaseModel):
    scenario_id: int
    return_scenario: ReturnScenario = ReturnScenario.BASE
    force_recompute: bool = False


class AccountBalancesOut(BaseModel):
    hysa: float
    brokerage: float
    roth_ira: float
    traditional_401k: float
    roth_401k: float
    total_pretax: float
    total_posttax: float
    total: float


class YearWithdrawalsOut(BaseModel):
    hysa: float
    brokerage: float
    roth_ira: float
    traditional_401k: float
    roth_401k: float
    roth_conversion: float


class YearContributionsOut(BaseModel):
    traditional_401k: float
    roth_401k: float
    roth_ira: float
    employer_match: float


class TaxSummaryOut(BaseModel):
    ordinary_taxable_income: float
    standard_deduction: float
    tax_owed: float
    effective_rate: float
    marginal_rate: float
    ltcg_tax: float
    niit: float
    ss_taxable_amount: float
    total_tax: float
    total_effective_rate: float


class ProjectionYearOut(BaseModel):
    calendar_year: int
    age_primary: int
    age_spouse: int | None
    phase: str
    balances_start: AccountBalancesOut
    balances_end: AccountBalancesOut
    contributions: YearContributionsOut
    withdrawals: YearWithdrawalsOut
    ss_primary: float
    ss_spouse: float
    healthcare_cost: float
    tax: TaxSummaryOut | None
    gross_income: float
    net_income: float
    income_target: float
    income_gap: float
    roth_ladder_conversion: float
    roth_available_principal: float
    is_depleted: bool
    notes: list[str]


class ProjectionResultOut(BaseModel):
    scenario_id: int
    return_scenario: ReturnScenario
    years: list[ProjectionYearOut]
    depletion_year: int | None
    depletion_age: int | None
    final_balance: float
    total_tax_paid: float
    total_ss_received: float
    success: bool


# ---------------------------------------------------------------------------
# Optimizer request/response
# ---------------------------------------------------------------------------


class OptimizerRequest(BaseModel):
    scenario_id: int
    primary_ss_claiming_ages: list[int] = Field(
        [62, 67, 70], description="Claiming ages to test for primary (years only)"
    )
    spouse_ss_claiming_ages: list[int] = Field(
        [62, 67, 70], description="Claiming ages to test for spouse"
    )
    roth_ladder_ceilings: list[float] = Field(
        [0.12, 0.22, 0.24], description="Bracket ceiling rates to test for Roth ladder"
    )
    optimize_against_scenario: ReturnScenario = ReturnScenario.BASE


class OptimizedStrategyOut(BaseModel):
    primary_ss_claim_age_years: int
    primary_ss_claim_age_months: int
    primary_ss_claim_label: str
    spouse_ss_claim_age_years: int | None
    spouse_ss_claim_age_months: int | None
    spouse_ss_claim_label: str | None
    roth_ladder_enabled: bool
    roth_ladder_target_bracket: float
    portfolio_survives: bool
    residual_balance: float
    total_tax_saved_vs_no_ladder: float | None
    rationale: list[str]
    projection: ProjectionResultOut


# ---------------------------------------------------------------------------
# Scenario full save/load (for scenario bar)
# ---------------------------------------------------------------------------


class FullScenarioOut(BaseModel):
    scenario: ScenarioOut
    assumptions: AssumptionsOut | None
    persons: list[PersonOut]
    accounts: list[AccountOut]
    contributions: list[ContributionOut]
    ss_claiming: list[SSClaimingOut]
    roth_overrides: list[RothConversionOut]


# ---------------------------------------------------------------------------
# Generic responses
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Contribution Planner
# ---------------------------------------------------------------------------


class ContributionPlanRequest(BaseModel):
    scenario_id: int
    return_scenario: ReturnScenario = ReturnScenario.BASE
    # Which accounts to include in the solve
    include_traditional_401k: bool = True
    include_roth_401k: bool = True
    include_roth_ira: bool = True
    include_hysa: bool = True
    include_brokerage: bool = True


class ContributionPlanResultOut(BaseModel):
    solved: bool
    traditional_401k_annual: float
    roth_401k_annual: float
    roth_ira_annual: float
    hysa_annual: float
    brokerage_annual: float
    employer_match_annual: float
    total_annual_contribution: float
    # IRS limits that applied (for UI display)
    limit_traditional_401k: float
    limit_roth_401k: float
    limit_roth_ira: float
    projection: ProjectionResultOut | None
    iterations: int
    notes: list[str]


# ---------------------------------------------------------------------------
# Generic responses
# ---------------------------------------------------------------------------


class MessageResponse(BaseModel):
    message: str


class ErrorResponse(BaseModel):
    detail: str
    code: str | None = None


class ErrorResponse(BaseModel):
    detail: str
    code: str | None = None
