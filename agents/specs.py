from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum

from domain.enums import RiskLevel
from domain.models import AgentSpec


class AgentRole(StrEnum):
    ARCHITECT = "architect"
    RESEARCHER = "researcher"
    CODER = "coder"
    TESTER = "tester"
    REVIEWER = "reviewer"
    DEBUGGER = "debugger"
    DOCUMENTATION = "documentation"
    VALIDATION = "validation"
    FINAL_REVIEWER = "final-reviewer"


def build_agent_specs(
    *, primary_model: str, fallback_models: tuple[str, ...] = ()
) -> dict[AgentRole, AgentSpec]:
    common = {
        "primary_model": primary_model,
        "fallback_models": fallback_models,
        "memory_policy": "project-procedure-and-prior-failures",
        "token_budget": 20_000,
        "cost_budget_usd": 2.0,
        "turn_budget": 12,
        "wall_time_seconds": 900,
        "sandbox_profile": "task-isolated",
        "network_profile": "none",
        "retry_policy": "transient-fallback-and-one-schema-repair",
        "escalation_policy": "human-on-policy-or-uncertain-side-effect",
        "termination_policy": "valid-output-or-budget-exhaustion",
    }
    definitions = {
        AgentRole.ARCHITECT: (
            "Decompose a goal into a validated dynamic task DAG.",
            "AgentInvocation@1.0",
            "TaskPlan@1.0",
            ("read_file", "search_code"),
            RiskLevel.LOW,
        ),
        AgentRole.RESEARCHER: (
            "Gather cited repository and external research evidence.",
            "AgentInvocation@1.0",
            "ResearchEvidence@1.0",
            ("read_file", "search_code", "web_search"),
            RiskLevel.LOW,
        ),
        AgentRole.CODER: (
            "Produce a typed implementation proposal and patch artifact.",
            "AgentInvocation@1.0",
            "ImplementationProposal@1.0",
            ("read_file", "search_code", "apply_patch", "run_tests"),
            RiskLevel.MEDIUM,
        ),
        AgentRole.TESTER: (
            "Generate and execute evidence-producing tests.",
            "AgentInvocation@1.0",
            "TestEvidence@1.0",
            ("read_file", "search_code", "apply_patch", "run_tests"),
            RiskLevel.MEDIUM,
        ),
        AgentRole.REVIEWER: (
            "Review implementation correctness, security, and maintainability.",
            "AgentInvocation@1.0",
            "ReviewFinding@1.0",
            ("read_file", "search_code", "run_tests"),
            RiskLevel.LOW,
        ),
        AgentRole.DEBUGGER: (
            "Diagnose verified failures and propose a bounded repair.",
            "AgentInvocation@1.0",
            "ImplementationProposal@1.0",
            ("read_file", "search_code", "apply_patch", "run_tests"),
            RiskLevel.MEDIUM,
        ),
        AgentRole.DOCUMENTATION: (
            "Produce accurate documentation grounded in verified artifacts.",
            "AgentInvocation@1.0",
            "ContextHandoff@1.0",
            ("read_file", "search_code", "apply_patch"),
            RiskLevel.LOW,
        ),
        AgentRole.VALIDATION: (
            "Inspect and verify task acceptance without mutating code.",
            "AgentInvocation@1.0",
            "ValidationResult@1.0",
            ("read_file", "search_code", "run_tests"),
            RiskLevel.LOW,
        ),
        AgentRole.FINAL_REVIEWER: (
            "Make the deterministic evidence-backed release decision.",
            "AgentInvocation@1.0",
            "ReleaseDecision@1.0",
            ("read_file", "git_diff", "run_tests"),
            RiskLevel.MEDIUM,
        ),
    }
    return {
        role: AgentSpec(
            role=role.value,
            purpose=purpose,
            input_schema=input_schema,
            output_schema=output_schema,
            tool_grants=tools,
            maximum_risk=risk,
            **common,
        )
        for role, (purpose, input_schema, output_schema, tools, risk) in definitions.items()
    }


def instantiate_required_agents[AgentT](
    roles: tuple[AgentRole, ...],
    specs: dict[AgentRole, AgentSpec],
    factory: Callable[[AgentRole, AgentSpec], AgentT],
) -> dict[AgentRole, AgentT]:
    result: dict[AgentRole, AgentT] = {}
    for role in roles:
        if role in result:
            continue
        try:
            spec = specs[role]
        except KeyError as error:
            raise LookupError(f"no AgentSpec is configured for {role.value}") from error
        result[role] = factory(role, spec)
    return result
