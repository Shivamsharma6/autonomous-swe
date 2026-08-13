from agents.base import AgentRuntime, get_default_agent_specs
from execution.scheduler.scheduler import TaskPlanner


class ArchitectAgent(AgentRuntime):
    """Architect Agent responsible for analyzing requirements and decomposing them into structured task DAGs."""

    def __init__(self):
        spec = get_default_agent_specs()["Architect"]
        super().__init__(spec)
        self.planner = TaskPlanner()

    def decompose_task(self, user_request: str):
        return self.planner.generate_dag(user_request)
