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


def _clean_code_block(text: str) -> str:
    if not text:
        return ""
    text = text.strip()
    import re
    # Extract code inside ```python ... ``` or ``` ... ``` if present anywhere in conversational text
    code_match = re.search(r"```(?:python|py)?\n(.*?)```", text, re.DOTALL)
    if code_match:
        return code_match.group(1).strip()

    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return text


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
                resolved_base_url = "http://host.docker.internal:11434/v1"
            elif config.provider in ("custom", "local", "unsloth", "omlx"):
                resolved_base_url = "http://host.docker.internal:8888/v1"
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
        import os
        import urllib.request
        import urllib.error

        config = self.get_effective_provider_config(provider_config)
        prompt = self.build_agent_prompt(task_goal, assembled_context)

        # 1. Google Gemini Cloud API attempt
        gemini_key = config.api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if config.provider in ("gemini", "google") and gemini_key:
            model_target = config.model_name if "gemini" in config.model_name else "gemini-1.5-flash"
            gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_target}:generateContent?key={gemini_key}"
            gemini_payload = {
                "contents": [
                    {
                        "role": "user",
                        "parts": [{"text": f"{self.spec.system_prompt}\n\n{prompt}"}]
                    }
                ],
                "generationConfig": {
                    "temperature": config.temperature
                }
            }
            try:
                req = urllib.request.Request(gemini_url, data=json.dumps(gemini_payload).encode("utf-8"), headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=45) as response:
                    res_json = json.loads(response.read().decode("utf-8"))
                    candidates = res_json.get("candidates", [])
                    if candidates:
                        parts = candidates[0].get("content", {}).get("parts", [])
                        if parts and "text" in parts[0]:
                            return _clean_code_block(parts[0]["text"])
            except Exception as e:
                pass

        # 2. Local / Ollama candidates setup
        candidates = []
        if config.base_url:
            raw_url = config.base_url.rstrip("/")
            candidates.append(raw_url)
            if "localhost" in raw_url:
                candidates.append(raw_url.replace("localhost", "host.docker.internal"))
            elif "127.0.0.1" in raw_url:
                candidates.append(raw_url.replace("127.0.0.1", "host.docker.internal"))
        else:
            candidates = [
                "http://host.docker.internal:11434/v1",
                "http://localhost:11434/v1",
                "http://127.0.0.1:11434/v1",
            ]

        # Resolve installed model name if local server is active
        target_model = config.model_name
        for base_url in candidates:
            models_url = f"{base_url}/models"
            try:
                m_req = urllib.request.Request(models_url)
                with urllib.request.urlopen(m_req, timeout=3) as m_res:
                    m_json = json.loads(m_res.read().decode("utf-8"))
                    installed = [m.get("id") or m.get("name") for m in m_json.get("data", []) if m.get("id") or m.get("name")]
                    if installed:
                        if target_model not in installed:
                            # Prefer coding or large local model
                            preferred = [m for m in installed if "coding" in m or "qwen" in m or "gemma" in m or "glm" in m]
                            target_model = preferred[0] if preferred else installed[0]
                        break
            except Exception:
                pass

        # Attempt completions across candidate endpoints
        for base_url in candidates:
            endpoint = f"{base_url}/chat/completions"
            payload = {
                "model": target_model,
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
                        if content and content.strip():
                            return _clean_code_block(content)
            except Exception:
                pass

            # Native Ollama /api/chat endpoint attempt
            native_endpoint = f"{base_url.replace('/v1', '').rstrip('/')}/api/chat"
            native_payload = {
                "model": target_model,
                "messages": [
                    {"role": "system", "content": self.spec.system_prompt},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
            }
            try:
                data_bytes = json.dumps(native_payload).encode("utf-8")
                req = urllib.request.Request(native_endpoint, data=data_bytes, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=60) as response:
                    res_json = json.loads(response.read().decode("utf-8"))
                    msg = res_json.get("message", {})
                    if isinstance(msg, dict) and msg.get("content"):
                        return _clean_code_block(msg["content"])
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

