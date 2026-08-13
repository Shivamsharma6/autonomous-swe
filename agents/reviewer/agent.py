from agents.base import AgentRuntime, get_default_agent_specs


class ReviewerAgent(AgentRuntime):
    """Reviewer Agent responsible for evaluating code quality and security compliance."""

    def __init__(self):
        spec = get_default_agent_specs()["Reviewer"]
        super().__init__(spec)
