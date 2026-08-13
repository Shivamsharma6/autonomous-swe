from agents.base import AgentRuntime, get_default_agent_specs


class ResearcherAgent(AgentRuntime):
    """Researcher Agent responsible for indexing workspace code and assembling context."""

    def __init__(self):
        spec = get_default_agent_specs()["Researcher"]
        super().__init__(spec)
