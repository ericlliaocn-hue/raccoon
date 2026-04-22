"""Executor 单元测试"""

import pytest

from src.executor.task_state_machine import TaskStateMachine, TransitionError
from src.executor.task_queue import TaskQueue
from src.types import Task, TaskStatus


# ─── TaskStateMachine ──────────────────────────────────────────

def test_valid_transition_pending_to_running():
    task = Task(
        conversation_id="test",
        origin_message="test",
        skill_name="echo",
    )
    assert task.status == TaskStatus.PENDING
    TaskStateMachine.transition(task, TaskStatus.RUNNING)
    assert task.status == TaskStatus.RUNNING


def test_valid_transition_running_to_success():
    task = Task(
        conversation_id="test",
        origin_message="test",
        skill_name="echo",
        status=TaskStatus.RUNNING,
    )
    TaskStateMachine.transition(task, TaskStatus.SUCCESS)
    assert task.status == TaskStatus.SUCCESS


def test_valid_transition_running_to_cancelling():
    task = Task(
        conversation_id="test",
        origin_message="test",
        skill_name="echo",
        status=TaskStatus.RUNNING,
    )
    TaskStateMachine.transition(task, TaskStatus.CANCELLING)
    assert task.status == TaskStatus.CANCELLING


def test_invalid_transition():
    task = Task(
        conversation_id="test",
        origin_message="test",
        skill_name="echo",
        status=TaskStatus.SUCCESS,
    )
    with pytest.raises(TransitionError):
        TaskStateMachine.transition(task, TaskStatus.RUNNING)


def test_cancel_pending_task():
    task = Task(
        conversation_id="test",
        origin_message="test",
        skill_name="echo",
    )
    TaskStateMachine.request_cancel(task)
    assert task.status == TaskStatus.CANCELLED
    assert task.cancel_token is True


def test_cancel_running_task():
    task = Task(
        conversation_id="test",
        origin_message="test",
        skill_name="echo",
        status=TaskStatus.RUNNING,
    )
    TaskStateMachine.request_cancel(task)
    assert task.status == TaskStatus.CANCELLING
    assert task.cancel_token is True


# ─── TaskQueue ─────────────────────────────────────────────────

@pytest.fixture
def task_queue(tmp_path):
    from src.config import RaccoonConfig
    config = RaccoonConfig(skills_dir=tmp_path / "skills")
    return TaskQueue(config)


def test_add_and_get(task_queue):
    task = Task(
        conversation_id="conv-1",
        origin_message="echo hello",
        skill_name="echo",
    )
    task_queue.add(task)
    assert task_queue.get(task.task_id) is not None
    assert task_queue.get(task.task_id).skill_name == "echo"


def test_get_by_conversation(task_queue):
    t1 = Task(conversation_id="conv-1", origin_message="a", skill_name="echo")
    t2 = Task(conversation_id="conv-1", origin_message="b", skill_name="echo")
    t3 = Task(conversation_id="conv-2", origin_message="c", skill_name="echo")
    task_queue.add(t1)
    task_queue.add(t2)
    task_queue.add(t3)

    result = task_queue.get_by_conversation("conv-1")
    assert len(result) == 2


def test_get_active_by_conversation(task_queue):
    t1 = Task(conversation_id="conv-1", origin_message="a", skill_name="echo")
    t2 = Task(conversation_id="conv-1", origin_message="b", skill_name="echo", status=TaskStatus.SUCCESS)
    task_queue.add(t1)
    task_queue.add(t2)

    result = task_queue.get_active_by_conversation("conv-1")
    assert len(result) == 1
    assert result[0].task_id == t1.task_id


def test_remove(task_queue):
    task = Task(conversation_id="conv-1", origin_message="a", skill_name="echo")
    task_queue.add(task)
    task_queue.remove(task.task_id)
    assert task_queue.get(task.task_id) is None


def test_active_count(task_queue):
    t1 = Task(conversation_id="c1", origin_message="a", skill_name="echo")
    t2 = Task(conversation_id="c2", origin_message="b", skill_name="echo", status=TaskStatus.RUNNING)
    t3 = Task(conversation_id="c3", origin_message="c", skill_name="echo", status=TaskStatus.SUCCESS)
    task_queue.add(t1)
    task_queue.add(t2)
    task_queue.add(t3)
    assert task_queue.active_count() == 2
