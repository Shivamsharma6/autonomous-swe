from typing import Dict
from pydantic import BaseModel, Field


class TokenCostTracker(BaseModel):
    """Tracks token consumption and estimated API usage costs."""

    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_cost_usd: float = 0.0

    PRICING_TABLE: Dict[str, Dict[str, float]] = Field(default_factory=lambda: {
        "nemotron-3.5-lightning:30b-mlx": {"prompt": 0.0001, "completion": 0.0004},
        "gemini-3.6-flash": {"prompt": 0.0001, "completion": 0.0004},
        "gemini-1.5-pro": {"prompt": 0.00125, "completion": 0.005},
        "gpt-4o": {"prompt": 0.0025, "completion": 0.010},
        "gpt-4o-mini": {"prompt": 0.00015, "completion": 0.0006},
        "claude-3-5-sonnet": {"prompt": 0.003, "completion": 0.015},
    })

    def record_usage(self, model_name: str, prompt_tokens: int, completion_tokens: int) -> float:
        self.total_prompt_tokens += prompt_tokens
        self.total_completion_tokens += completion_tokens

        rates = self.PRICING_TABLE.get(model_name, {"prompt": 0.0005, "completion": 0.0015})
        cost = (prompt_tokens / 1000.0 * rates["prompt"]) + (completion_tokens / 1000.0 * rates["completion"])
        self.total_cost_usd += cost
        return cost
