from agents.base import AgentRuntime, get_default_agent_specs


class TesterAgent(AgentRuntime):
    """Tester Agent responsible for generating unit tests and pytest suites."""

    def __init__(self):
        spec = get_default_agent_specs()["Tester"]
        super().__init__(spec)
