import queue
import threading
from types import SimpleNamespace
from uuid import uuid4

from execution.repositories import CommandSpec
from execution.sandbox.policy import SandboxPolicy
from execution.sandbox.runner import DockerSandboxRunner, SandboxRequest
from tests.unit.sandbox.test_policy import valid_policy


class ImmediateContainer:
    def __init__(self):
        self.attrs = {"State": {"Status": "created", "Running": False, "ExitCode": 0}}
        self.frames = None

    def reload(self):
        pass

    def start(self):
        self.attrs["State"]["Status"] = "exited"
        if self.frames is not None:
            self.frames.put((b"telemetry\n", None))
            self.frames.put(None)

    def attach(self, **kwargs):
        self.frames = queue.Queue()
        if self.attrs["State"]["Status"] == "exited":
            self.frames.put(None)
        return iter(self.frames.get, None)

    def stats(self, **kwargs):
        return {
            "cpu_stats": {"cpu_usage": {"total_usage": 0}},
            "memory_stats": {"usage": 1024},
            "pids_stats": {"current": 1},
        }


class LateOutputContainer(ImmediateContainer):
    def __init__(self):
        super().__init__()
        self.after_exit = threading.Event()

    def start(self):
        self.attrs["State"].update(Status="running", Running=True)

    def attach(self, **kwargs):
        assert self.after_exit.wait(2)
        yield (b"abcdefghijklmnopqrst", None)

    def reload(self):
        if self.attrs["State"]["Running"]:
            self.attrs["State"].update(Status="exited", Running=False)
            self.after_exit.set()


def run_container(tmp_path, container, **overrides):
    source, worktree = tmp_path / "source", tmp_path / "worktree"
    source.mkdir()
    worktree.mkdir()
    request = SandboxRequest(
        execution_id=uuid4(),
        run_id=uuid4(),
        task_id=uuid4(),
        attempt_id=uuid4(),
        source_repository=source,
        worktree=worktree,
        command=CommandSpec(argv=("python", "-c", "print('telemetry')"), timeout_seconds=5),
        policy=SandboxPolicy.model_validate(valid_policy(**overrides)),
    )
    client = SimpleNamespace(containers=SimpleNamespace(get=lambda _: container))
    return DockerSandboxRunner(client).run_created(request, "container")


def test_immediate_output_is_captured_even_before_log_driver_flush(tmp_path):
    result = run_container(tmp_path, ImmediateContainer())
    assert result.execution.stdout_bytes == 10
    assert result.stdout == "telemetry\n"


def test_output_limit_in_final_drain_cannot_report_success(tmp_path):
    result = run_container(
        tmp_path, LateOutputContainer(), max_stdout_bytes=10, max_total_output_bytes=1_000_010
    )
    assert result.execution.stdout_bytes == 20
    assert result.stdout == "abcdefghij"
    assert result.stdout_truncated
    assert result.execution.exit_reason == "OUTPUT_LIMIT"
    assert result.execution.limit_triggered == "output_bytes"
