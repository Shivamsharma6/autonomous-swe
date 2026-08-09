import time
import pytest
from autoswe.models import TaskNode, TaskStatus
from autoswe.scheduler import TaskPlanner, TaskScheduler


def test_task_planner_generate_dag():
    planner = TaskPlanner()
    nodes = planner.generate_dag("Implement user authentication system")
    
    assert isinstance(nodes, list)
    assert len(nodes) >= 3
    
    node_ids = {n.id for n in nodes}
    for node in nodes:
        assert isinstance(node, TaskNode)
        assert node.assigned_agent is not None
        # All dependencies must refer to existing node IDs in the DAG
        for dep in node.dependencies:
            assert dep in node_ids
    
    # First node should have no dependencies
    assert len(nodes[0].dependencies) == 0


def test_task_scheduler_registration_and_ready_tasks():
    scheduler = TaskScheduler()
    t1 = TaskNode(id="t1", title="Task 1", assigned_agent="Researcher")
    t2 = TaskNode(id="t2", title="Task 2", assigned_agent="Coder", dependencies=["t1"])
    
    scheduler.register_task(t1)
    scheduler.register_task(t2)
    
    # t1 has no dependencies, so it should be READY
    ready = scheduler.get_ready_tasks()
    assert len(ready) == 1
    assert ready[0].id == "t1"
    assert scheduler.get_task_status("t1") == TaskStatus.READY
    assert scheduler.get_task_status("t2") == TaskStatus.PENDING


def test_task_scheduler_leasing_and_heartbeats():
    scheduler = TaskScheduler(lease_ttl_sec=1.0)
    t1 = TaskNode(id="t1", title="Task 1", assigned_agent="Coder")
    scheduler.register_task(t1)
    
    # Ready tasks
    ready = scheduler.get_ready_tasks()
    assert len(ready) == 1
    
    # Lease task
    leased_node = scheduler.lease_task("t1", worker_id="worker-A")
    assert leased_node is not None
    assert leased_node.id == "t1"
    assert scheduler.get_task_status("t1") == TaskStatus.LEASED
    
    # Cannot lease already leased task
    assert scheduler.lease_task("t1", worker_id="worker-B") is None
    
    # Heartbeat from correct worker succeeds
    assert scheduler.send_heartbeat("t1", worker_id="worker-A") is True
    
    # Heartbeat from wrong worker fails
    assert scheduler.send_heartbeat("t1", worker_id="worker-B") is False
    
    # Heartbeat for non-existent task fails
    assert scheduler.send_heartbeat("non-existent", worker_id="worker-A") is False


def test_task_scheduler_reclaim_expired_leases():
    scheduler = TaskScheduler(lease_ttl_sec=0.2)
    t1 = TaskNode(id="t1", title="Task 1", assigned_agent="Coder")
    scheduler.register_task(t1)
    
    scheduler.get_ready_tasks()
    scheduler.lease_task("t1", worker_id="worker-A")
    assert scheduler.get_task_status("t1") == TaskStatus.LEASED
    
    # Wait for lease to expire
    time.sleep(0.3)
    
    scheduler.reclaim_expired_leases()
    assert scheduler.get_task_status("t1") == TaskStatus.READY
    
    # Now worker-B should be able to lease it
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
    
    # Complete t1
    scheduler.complete_task("t1")
    assert scheduler.get_task_status("t1") == TaskStatus.COMPLETED
    
    # t2 should now become READY
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
    
    # Heartbeat on cancelled task should fail
    assert scheduler.send_heartbeat("t1", worker_id="worker-A") is False
