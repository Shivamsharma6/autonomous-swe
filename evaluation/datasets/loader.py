from typing import Any


class DatasetLoader:
    """Loader for software engineering evaluation datasets and benchmark problems."""

    def load_swe_bench_dataset(self) -> list[dict[str, Any]]:
        """Return sample SWE benchmark tasks."""
        return [
            {
                "instance_id": "swe-bench-001",
                "problem_statement": "Implement TicTacToe class with make_move and check_winner",
                "repo": "demo/tictactoe",
            }
        ]
