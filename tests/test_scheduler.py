import threading
import time
import pytest
from execution.scheduler.scheduler import TaskNode, TaskStatus, TaskPlanner, TaskScheduler


def test_task_planner_generate_dag():
    planner = TaskPlanner()
    nodes = planner.generate_dag("Implement user authentication system")

    assert isinstance(nodes, list)
    assert len(nodes) >= 3

    node_ids = {n.id for n in nodes}
    for node in nodes:
        assert isinstance(node, TaskNode)
        assert node.assigned_agent is not None
        for dep in node.dependencies:
            assert dep in node_ids

    assert len(nodes[0].dependencies) == 0


def test_task_scheduler_registration_and_ready_tasks():
    scheduler = TaskScheduler()
    t1 = TaskNode(id="t1", title="Task 1", assigned_agent="Researcher")
    t2 = TaskNode(id="t2", title="Task 2", assigned_agent="Coder", dependencies=["t1"])

    scheduler.register_task(t1)
    scheduler.register_task(t2)

    ready = scheduler.get_ready_tasks()
    assert len(ready) == 1
    assert ready[0].id == "t1"
    assert scheduler.get_task_status("t1") == TaskStatus.READY
    assert scheduler.get_task_status("t2") == TaskStatus.PENDING


def test_task_scheduler_leasing_and_heartbeats():
    scheduler = TaskScheduler(lease_ttl_sec=1.0)
    t1 = TaskNode(id="t1", title="Task 1", assigned_agent="Coder")
    scheduler.register_task(t1)

    ready = scheduler.get_ready_tasks()
    assert len(ready) == 1

    leased_node = scheduler.lease_task("t1", worker_id="worker-A")
    assert leased_node is not None
    assert leased_node.id == "t1"
    assert scheduler.get_task_status("t1") == TaskStatus.LEASED

    assert scheduler.lease_task("t1", worker_id="worker-B") is None
    assert scheduler.send_heartbeat("t1", worker_id="worker-A") is True
    assert scheduler.send_heartbeat("t1", worker_id="worker-B") is False
    assert scheduler.send_heartbeat("non-existent", worker_id="worker-A") is False


def test_task_scheduler_reclaim_expired_leases():
    scheduler = TaskScheduler(lease_ttl_sec=0.2)
    t1 = TaskNode(id="t1", title="Task 1", assigned_agent="Coder")
    scheduler.register_task(t1)

    scheduler.get_ready_tasks()
    scheduler.lease_task("t1", worker_id="worker-A")
    assert scheduler.get_task_status("t1") == TaskStatus.LEASED

    time.sleep(0.3)

    scheduler.reclaim_expired_leases()
    assert scheduler.get_task_status("t1") == TaskStatus.READY

    leased_again = scheduler.lease_task("t1", worker_id="worker-B")
    assert leased_again is not None


def test_task_scheduler_complete_and_dependency_unblocking():
    scheduler = TaskScheduler()
    t1 = TaskNode(id="t1", title="Task 1", assigned_agent="Researcher")
    t2 = TaskNode(id="t2", title="Task 2", assigned_agent="Coder", dependencies=["t1"])

    scheduler.register_task(t1)
    scheduler.register_task(t2)

    scheduler.get_ready_tasks()
    scheduler.lease_task("t1", worker_id="worker-A")

    scheduler.complete_task("t1")
    assert scheduler.get_task_status("t1") == TaskStatus.COMPLETED

    ready = scheduler.get_ready_tasks()
    assert len(ready) == 1
    assert ready[0].id == "t2"
    assert scheduler.get_task_status("t2") == TaskStatus.READY


def test_task_scheduler_cancellation():
    scheduler = TaskScheduler()
    t1 = TaskNode(id="t1", title="Task 1", assigned_agent="Coder")
    scheduler.register_task(t1)

    scheduler.get_ready_tasks()
    scheduler.lease_task("t1", worker_id="worker-A")

    scheduler.cancel_task("t1")
    assert scheduler.get_task_status("t1") == TaskStatus.CANCELLED

    assert scheduler.send_heartbeat("t1", worker_id="worker-A") is False


def test_task_scheduler_concurrent_leasing():
    scheduler = TaskScheduler()
    t1 = TaskNode(id="t1", title="Concurrent Task", assigned_agent="Coder")
    scheduler.register_task(t1)

    ready = scheduler.get_ready_tasks()
    assert len(ready) == 1

    num_threads = 10
    barrier = threading.Barrier(num_threads)
    results = []

    def worker(worker_idx: int):
        barrier.wait()
        res = scheduler.lease_task("t1", worker_id=f"worker-{worker_idx}")
        results.append(res)

    threads = []
    for i in range(num_threads):
        t = threading.Thread(target=worker, args=(i,))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    successful_leases = [r for r in results if r is not None]
    assert len(successful_leases) == 1
    assert scheduler.get_task_status("t1") == TaskStatus.LEASED
