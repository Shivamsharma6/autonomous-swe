from agents.base import AgentRuntime, get_default_agent_specs


class CoderAgent(AgentRuntime):
    """Coder Agent responsible for writing feature implementations and source code."""

    def __init__(self):
        spec = get_default_agent_specs()["Coder"]
        super().__init__(spec)
