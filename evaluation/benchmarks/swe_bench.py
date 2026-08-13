from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from evaluation.datasets.loader import DatasetLoader
from evaluation.evaluators.metrics import EvaluatorMetrics

BenchmarkExecutor = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class SWEBenchmarkRunner:
    """Benchmark runner suite for autonomous SWE evaluations."""

    def __init__(
        self,
        executor: BenchmarkExecutor,
        *,
        loader: DatasetLoader | None = None,
        metrics: EvaluatorMetrics | None = None,
    ) -> None:
        """Use the same production API/client path that real runs use.

        The benchmark owns no planner, scheduler, or workflow implementation. Callers must
        inject an executor that submits a case to the authoritative production engine and
        waits for its terminal evidence.
        """
        self._executor = executor
        self.loader = loader or DatasetLoader()
        self.metrics = metrics or EvaluatorMetrics()

    async def run_benchmark_suite(self) -> dict[str, Any]:
        dataset = self.loader.load_swe_bench_dataset()
        resolved_count = 0
        total = len(dataset)

        for item in dataset:
            res = await self._executor(item)
            test_res = res.get("test_res", {})
            eval_res = self.metrics.evaluate_task(test_res)
            if eval_res.get("is_resolved"):
                resolved_count += 1

        return {
            "total_tasks": total,
            "resolved_tasks": resolved_count,
            "benchmark_score": (resolved_count / total * 100.0) if total > 0 else 0.0,
        }
