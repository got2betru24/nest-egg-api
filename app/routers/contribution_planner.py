# =============================================================================
# NestEgg - routers/contribution_planner.py
# Contribution planner endpoint — solves for minimum annual contributions
# needed to survive to plan_to_age.
# =============================================================================

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..models import ContributionPlanRequest, ContributionPlanResultOut
from ..engine.contribution_planner import AccountInclusion, solve_contributions
from ..engine.projection import ReturnScenario
from .projection import _build_projection_inputs, _result_to_out

router = APIRouter(prefix="/contribution-planner", tags=["contribution-planner"])


@router.post("/solve", response_model=ContributionPlanResultOut)
async def solve_contribution_plan(body: ContributionPlanRequest):
    from ..database import fetchone
    scenario = await fetchone(
        "SELECT id FROM scenarios WHERE id = %s AND is_active = 1",
        (body.scenario_id,),
    )
    if not scenario:
        raise HTTPException(status_code=404, detail="Scenario not found")

    base_inputs = await _build_projection_inputs(
        body.scenario_id, body.return_scenario
    )

    inclusion = AccountInclusion(
        traditional_401k=body.include_traditional_401k,
        roth_401k=body.include_roth_401k,
        roth_ira=body.include_roth_ira,
        hysa=body.include_hysa,
        brokerage=body.include_brokerage,
    )

    plan = solve_contributions(
        base_inputs=base_inputs,
        inclusion=inclusion,
        scenario=ReturnScenario(body.return_scenario.value),
    )

    proj_out = _result_to_out(
        body.scenario_id,
        body.return_scenario,
        plan.projection,
    ) if plan.projection else None

    return {
        "solved": plan.solved,
        "traditional_401k_annual": plan.traditional_401k_annual,
        "roth_401k_annual": plan.roth_401k_annual,
        "roth_ira_annual": plan.roth_ira_annual,
        "hysa_annual": plan.hysa_annual,
        "brokerage_annual": plan.brokerage_annual,
        "employer_match_annual": plan.employer_match_annual,
        "total_annual_contribution": plan.total_annual_contribution,
        "limit_traditional_401k": plan.limit_traditional_401k,
        "limit_roth_401k": plan.limit_roth_401k,
        "limit_roth_ira": plan.limit_roth_ira,
        "projection": proj_out,
        "iterations": plan.iterations,
        "notes": plan.notes,
    }
