from typing import Any, Dict


class EvaluatorMetrics:
    """Evaluates resolution status, test pass rates, and patch quality."""

    def evaluate_task(self, test_results: Dict[str, Any]) -> Dict[str, Any]:
        exit_code = test_results.get("exit_code", -1)
        passed = exit_code == 0
        return {
            "is_resolved": passed,
            "pass_rate": 100.0 if passed else 0.0,
            "test_exit_code": exit_code,
        }
