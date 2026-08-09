from typing import Dict, Optional, Any
from autoswe.models import AgentSpec, RiskLevel


def get_default_agent_specs() -> Dict[str, AgentSpec]:
    return {
        "Architect": AgentSpec(
            name="Architect",
            role="Architect",
            description="Decomposes requirements into structured task DAGs",
            system_prompt="You are the Architect Agent responsible for analyzing requirements and decomposing them into structured task DAGs.",
            tools=["list_dir", "read_file"],
            risk_level=RiskLevel.LOW,
        ),
        "Researcher": AgentSpec(
            name="Researcher",
            role="Researcher",
            description="Indexes codebase AST and retrieves relevant context",
            system_prompt="You are the Researcher Agent responsible for indexing the codebase and retrieving relevant context.",
            tools=["search_code", "read_file"],
            risk_level=RiskLevel.LOW,
        ),
        "Coder": AgentSpec(
            name="Coder",
            role="Coder",
            description="Writes code feature implementations and updates files",
            system_prompt="You are the Coder Agent responsible for implementing features and writing clean source code.",
            tools=["write_file", "read_file"],
            risk_level=RiskLevel.MEDIUM,
        ),
        "Tester": AgentSpec(
            name="Tester",
            role="Test Generator",
            description="Generates comprehensive pytest unit tests and mocks",
            system_prompt="You are the Tester Agent responsible for writing unit tests, test suites, and mock objects.",
            tools=["write_file", "read_file", "pytest"],
            risk_level=RiskLevel.LOW,
        ),
        "Reviewer": AgentSpec(
            name="Reviewer",
            role="Reviewer",
            description="Evaluates code quality, lint status, and security compliance",
            system_prompt="You are the Reviewer Agent responsible for auditing code quality, lint status, and security compliance.",
            tools=["read_file", "pytest"],
            risk_level=RiskLevel.LOW,
        ),
        "Debugger": AgentSpec(
            name="Debugger",
            role="Debugger",
            description="Parses stack traces and implements self-healing code fixes",
            system_prompt="You are the Debugger Agent responsible for parsing stack traces and implementing fixes for broken tests or errors.",
            tools=["write_file", "read_file", "pytest"],
            risk_level=RiskLevel.MEDIUM,
        ),
        "Final Reviewer": AgentSpec(
            name="Final Reviewer",
            role="Final Reviewer",
            description="Evaluates completed feature and prepares Git Pull Request",
            system_prompt="You are the Final Reviewer Agent responsible for verifying completed tasks and finalizing the output.",
            tools=["git_diff", "git_commit"],
            risk_level=RiskLevel.MEDIUM,
        ),
    }


class AgentRuntime:
    def __init__(self, spec: AgentSpec):
        self.spec = spec

    def build_agent_prompt(self, task_goal: str, assembled_context: str = "") -> str:
        sections = [
            f"System: You are the {self.spec.role} Agent ({self.spec.name}).",
            f"Role Description: {self.spec.description}",
        ]
        if self.spec.system_prompt:
            sections.append(f"System Instructions: {self.spec.system_prompt}")
        if self.spec.tools:
            sections.append(f"Allowed Tools: {', '.join(self.spec.tools)}")
        
        sections.append(f"\nUser Goal: {task_goal}")
        
        if assembled_context:
            sections.append(f"\n{assembled_context}")
            
        return "\n".join(sections)
