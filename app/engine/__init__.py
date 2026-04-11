# NestEgg engine package
from .inflation import inflate, deflate, real_return, build_income_schedule
from .contribution_limits import get_limits, validate_contributions, LimitRow, AnnualLimits
from .tax_engine import compute_total_tax, compute_ordinary_tax, bracket_fill_at, TaxYear, BracketRow
from .social_security import estimate_benefit, build_claiming_comparison, compute_spousal_benefit
from .bridge_strategy import build_bridge_strategy, BridgeInput, BridgeMethod
from .roth_ladder import build_ladder_schedule, LadderState, optimal_conversion_amount
from .projection import run_projection, ProjectionInputs, ProjectionResult, ReturnScenario
from .optimizer import run_optimizer, OptimizerConfig, OptimizedStrategy

__all__ = [
    "inflate", "deflate", "real_return", "build_income_schedule",
    "get_limits", "validate_contributions", "LimitRow", "AnnualLimits",
    "compute_total_tax", "compute_ordinary_tax", "bracket_fill_at", "TaxYear", "BracketRow",
    "estimate_benefit", "build_claiming_comparison", "compute_spousal_benefit",
    "build_bridge_strategy", "BridgeInput", "BridgeMethod",
    "build_ladder_schedule", "LadderState", "optimal_conversion_amount",
    "run_projection", "ProjectionInputs", "ProjectionResult", "ReturnScenario",
    "run_optimizer", "OptimizerConfig", "OptimizedStrategy",
]
