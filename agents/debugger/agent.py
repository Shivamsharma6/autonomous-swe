from agents.base import AgentRuntime, get_default_agent_specs


class DebuggerAgent(AgentRuntime):
    """Debugger Agent responsible for stack trace analysis and self-healing fixes."""

    def __init__(self):
        spec = get_default_agent_specs()["Debugger"]
        super().__init__(spec)
