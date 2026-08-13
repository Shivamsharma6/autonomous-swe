import json
import os
import re
import ast
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
from policies.risk.policy_engine import RiskLevel

try:
    from langsmith import traceable
except Exception:
    def traceable(name=None, **kwargs):
        def decorator(func):
            return func
        return decorator


class ModelProviderConfig(BaseModel):
    """Configuration for an LLM provider."""

    provider: str = "custom"  # gemini, google, openai, anthropic, ollama, custom, local
    model_name: str = "nemotron-3.5-lightning:30b-mlx"
    base_url: str = ""
    api_key: str = ""
    temperature: float = 0.2

    def resolved_base_url(self) -> str:
        url = self.base_url or ""
        if "localhost" in url:
            url = url.replace("localhost", "host.docker.internal")
        elif "127.0.0.1" in url:
            url = url.replace("127.0.0.1", "host.docker.internal")
        elif "host.docker.internal" in url:
            url = url.replace("host.docker.internal", "localhost")
        return url

    def model_dump(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "model_name": self.model_name,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "temperature": self.temperature,
        }


class AgentSpec(BaseModel):
    """Specification for an agent in the workflow."""

    name: str
    role: str
    description: str = ""
    system_prompt: str = ""
    tools: List[str] = Field(default_factory=list)
    model: str = "nemotron-3.5-lightning:30b-mlx"
    provider_config: Optional[ModelProviderConfig] = None
    risk_level: RiskLevel = RiskLevel.LOW

    def build_system_prompt(self) -> str:
        tools_str = ", ".join(self.tools) if self.tools else "None"
        return f"{self.system_prompt}\nAvailable tools: {tools_str}"


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
            system_prompt="You are the Coder Agent responsible for implementing features. CRITICAL: Output ONLY valid executable Python code enclosed in a ```python ... ``` block. Do NOT include plan descriptions, conversational commentary, or text outside the code block.",
            tools=["write_file", "read_file"],
            risk_level=RiskLevel.MEDIUM,
        ),
        "Tester": AgentSpec(
            name="Tester",
            role="Test Generator",
            description="Generates comprehensive pytest unit tests and mocks",
            system_prompt="You are the Tester Agent responsible for writing unit tests. CRITICAL: Output ONLY valid executable Python code enclosed in a ```python ... ``` block. All imports MUST import directly from `utils`.",
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
            system_prompt="You are the Debugger Agent. CRITICAL: Output ONLY valid executable Python code enclosed in a ```python ... ``` block fixing the error.",
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


def _clean_code_block(text: str) -> str:
    if not text:
        return "# Code generated by Agent\n"
    text = text.strip()

    # 1. Try finding markdown python code block
    code_match = re.search(r"```(?:python|py)?\n(.*?)```", text, re.DOTALL)
    candidate = code_match.group(1).strip() if code_match else text

    # Strip residual markdown ```
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()

    # Validate AST syntax
    try:
        ast.parse(candidate)
        return candidate
    except SyntaxError:
        pass

    # 2. Search for any valid code blocks in conversational text
    blocks = re.findall(r"```(?:python|py)?\n(.*?)```", text, re.DOTALL)
    for b in blocks:
        b_clean = b.strip()
        try:
            ast.parse(b_clean)
            return b_clean
        except SyntaxError:
            continue

    return "# Code generated by Agent\n"


class AgentRuntime:
    """Runtime engine for executing individual AI agents with provider flexibility."""

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

    @traceable(name="Agent-LLM-Completion", run_type="llm")
    def generate_completion(
        self,
        task_goal: str,
        assembled_context: str = "",
        provider_config: Optional[ModelProviderConfig] = None,
    ) -> str:
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
                        "parts": [{"text": f"{self.spec.system_prompt}\n\n{prompt}"}],
                    }
                ],
                "generationConfig": {"temperature": config.temperature},
            }
            try:
                req = urllib.request.Request(
                    gemini_url,
                    data=json.dumps(gemini_payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=45) as response:
                    res_json = json.loads(response.read().decode("utf-8"))
                    candidates = res_json.get("candidates", [])
                    if candidates:
                        parts = candidates[0].get("content", {}).get("parts", [])
                        if parts and "text" in parts[0]:
                            return _clean_code_block(parts[0]["text"])
            except Exception:
                pass

        # 2. Local / Ollama candidate setup
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
                            preferred = [m for m in installed if "coding" in m or "qwen" in m or "gemma" in m or "glm" in m]
                            target_model = preferred[0] if preferred else installed[0]
                        break
            except Exception:
                pass

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

        return f"# Code generated by {self.spec.role} for task: {task_goal}\n"
