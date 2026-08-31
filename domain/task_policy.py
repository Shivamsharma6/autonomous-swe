"""Static execution grants, independent of model-selected tool calls."""

from domain.enums import RiskLevel, TaskType

TASK_CAPABILITY_NAMES = {
    TaskType.RESEARCH: ("research", "researcher", "analysis"),
    TaskType.IMPLEMENTATION: ("implementation", "coder"),
    TaskType.TEST: ("test", "testing", "tester"),
    TaskType.REFACTOR: ("refactor", "refactoring"),
    TaskType.DOCUMENTATION: ("documentation",),
    TaskType.VALIDATION: ("validation",),
}

TASK_REQUIRED_TOOLS = {
    TaskType.RESEARCH: frozenset({"read_file", "search_code"}),
    TaskType.IMPLEMENTATION: frozenset({"read_file", "apply_patch", "run_tests"}),
    TaskType.TEST: frozenset({"read_file", "apply_patch", "run_tests"}),
    TaskType.REFACTOR: frozenset({"read_file", "apply_patch", "run_tests"}),
    TaskType.DOCUMENTATION: frozenset({"read_file", "apply_patch", "run_tests"}),
    TaskType.VALIDATION: frozenset({"read_file", "run_tests"}),
}


def task_minimum_risk(task_type: TaskType) -> RiskLevel:
    return RiskLevel.LOW if task_type is TaskType.RESEARCH else RiskLevel.MEDIUM


def capabilities_for_assignment(assignment: str) -> frozenset[str]:
    # Expand a persisted task assignment through an explicit policy. Never
    # manufacture a capability from the tool the model is trying to call.
    for task_type, names in TASK_CAPABILITY_NAMES.items():
        if assignment in names:
            if task_type is TaskType.RESEARCH:
                return frozenset({"repository-read"})
            if task_type is TaskType.VALIDATION:
                return frozenset({"repository-read", "verification"})
            return frozenset({"repository-read", "repository-write", "verification"})
    # Preserve explicit primitive grants without escalating them.
    return frozenset({assignment})


def planner_execution_contract() -> dict[str, object]:
    return {
        task_type.value: {
            "assigned_capability": names[0],
            "required_tools": sorted(TASK_REQUIRED_TOOLS[task_type]),
            "minimum_risk": task_minimum_risk(task_type).value,
        }
        for task_type, names in TASK_CAPABILITY_NAMES.items()
    }
