from typing import Dict, Optional, Any
from autoswe.models import AgentSpec, RiskLevel, ModelProviderConfig


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


try:
    from langsmith import traceable
except Exception:
    def traceable(name=None, **kwargs):
        def decorator(func):
            return func
        return decorator


class AgentRuntime:
    """Runtime engine for executing individual AI agents with model provider flexibility."""

    def __init__(
        self,
        spec: AgentSpec,
        default_provider_config: Optional[ModelProviderConfig] = None,
    ):
        self.spec = spec
        self.default_provider_config = default_provider_config or ModelProviderConfig(
            provider="gemini",
            model_name="gemini-3.6-flash",
            temperature=0.2,
        )

    def get_effective_provider_config(
        self, override_config: Optional[ModelProviderConfig] = None
    ) -> ModelProviderConfig:
        return override_config or self.default_provider_config

    def build_system_prompt(self) -> str:
        tools_str = ", ".join(self.spec.tools) if self.spec.tools else "None"
        return f"{self.spec.system_prompt}\nAvailable tools: {tools_str}"

    def inspect_state(
        self,
        task_goal: str,
        assembled_context: str = "",
        provider_config: Optional[ModelProviderConfig] = None,
    ) -> Dict[str, Any]:
        config = self.get_effective_provider_config(provider_config)
        prompt = self.build_agent_prompt(task_goal, assembled_context)

        if config.base_url:
            resolved_base_url = config.base_url
        else:
            if config.provider == "ollama":
                resolved_base_url = "http://localhost:11434/v1"
            elif config.provider in ("custom", "local", "unsloth", "omlx"):
                resolved_base_url = "http://localhost:8888/v1"
            else:
                resolved_base_url = ""

        return {
            "provider": config.provider,
            "model_name": config.model_name,
            "base_url": resolved_base_url,
            "api_key_configured": bool(config.api_key),
            "prompt": prompt,
            "status": "ready",
        }

    @traceable(name="Agent-LLM-Completion", run_type="llm")
    def generate_completion(
        self,
        task_goal: str,
        assembled_context: str = "",
        provider_config: Optional[ModelProviderConfig] = None,
    ) -> str:
        import json
        import urllib.request
        import urllib.error

        config = self.get_effective_provider_config(provider_config)
        prompt = self.build_agent_prompt(task_goal, assembled_context)

        raw_base_url = config.base_url.rstrip("/")
        if not raw_base_url:
            if config.provider == "ollama":
                raw_base_url = "http://localhost:11434/v1"
            elif config.provider in ("custom", "local", "unsloth", "omlx"):
                raw_base_url = "http://localhost:8888/v1"

        candidates = []
        if raw_base_url:
            candidates.append(raw_base_url)
            if "localhost" in raw_base_url:
                candidates.append(raw_base_url.replace("localhost", "host.docker.internal"))
            elif "127.0.0.1" in raw_base_url:
                candidates.append(raw_base_url.replace("127.0.0.1", "host.docker.internal"))
            elif "host.docker.internal" in raw_base_url:
                candidates.append(raw_base_url.replace("host.docker.internal", "localhost"))

        for base_url in candidates:
            # 1. OpenAI-compatible /chat/completions endpoint
            endpoint = f"{base_url}/chat/completions"
            if not endpoint.endswith("/v1/chat/completions") and "/v1" not in base_url and config.provider == "ollama":
                endpoint = f"{base_url}/v1/chat/completions"

            payload = {
                "model": config.model_name,
                "messages": [
                    {"role": "system", "content": self.spec.system_prompt},
                    {"role": "user", "content": prompt},
                ],
                "temperature": config.temperature,
            }
            data_bytes = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(endpoint, data=data_bytes, headers={"Content-Type": "application/json"})
            if config.api_key:
                req.add_header("Authorization", f"Bearer {config.api_key}")

            try:
                with urllib.request.urlopen(req, timeout=60) as response:
                    res_json = json.loads(response.read().decode("utf-8"))
                    choices = res_json.get("choices", [])
                    if choices:
                        content = choices[0].get("message", {}).get("content", "")
                        if content:
                            return content
            except Exception:
                pass

            # 2. Native Ollama /api/chat endpoint
            if config.provider == "ollama" or "11434" in base_url:
                native_endpoint = f"{base_url.replace('/v1', '').rstrip('/')}/api/chat"
                native_payload = {
                    "model": config.model_name,
                    "messages": [
                        {"role": "system", "content": self.spec.system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                    "stream": False,
                }
                data_bytes = json.dumps(native_payload).encode("utf-8")
                req = urllib.request.Request(native_endpoint, data=data_bytes, headers={"Content-Type": "application/json"})
                try:
                    with urllib.request.urlopen(req, timeout=60) as response:
                        res_json = json.loads(response.read().decode("utf-8"))
                        msg = res_json.get("message", {})
                        if isinstance(msg, dict) and msg.get("content"):
                            return msg["content"]
                except Exception:
                    pass

        return f"# Code generated by {self.spec.role} for task: {task_goal}\n"

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

