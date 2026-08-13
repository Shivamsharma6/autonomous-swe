from typing import Any, Dict, List
from evaluation.datasets.loader import DatasetLoader
from evaluation.evaluators.metrics import EvaluatorMetrics
from workflows.feature import WorkflowOrchestrator


class SWEBenchmarkRunner:
    """Benchmark runner suite for autonomous SWE evaluations."""

    def __init__(self):
        self.loader = DatasetLoader()
        self.metrics = EvaluatorMetrics()

    def run_benchmark_suite(self) -> Dict[str, Any]:
        dataset = self.loader.load_swe_bench_dataset()
        resolved_count = 0
        total = len(dataset)

        for item in dataset:
            orchestrator = WorkflowOrchestrator()
            res = orchestrator.run_workflow(user_request=item["problem_statement"])
            test_res = res.get("test_res", {})
            eval_res = self.metrics.evaluate_task(test_res)
            if eval_res.get("is_resolved"):
                resolved_count += 1

        return {
            "total_tasks": total,
            "resolved_tasks": resolved_count,
            "benchmark_score": (resolved_count / total * 100.0) if total > 0 else 0.0,
        }
