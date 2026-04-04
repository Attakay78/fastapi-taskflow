from datetime import datetime

from fastapi_taskflow.models import TaskConfig, TaskRecord, TaskStatus


def test_task_record_duration_none_when_not_finished():
    record = TaskRecord(task_id="t1", func_name="f", status=TaskStatus.PENDING)
    assert record.duration is None


def test_task_record_duration_computed():
    start = datetime(2024, 1, 1, 0, 0, 0)
    end = datetime(2024, 1, 1, 0, 0, 3)
    record = TaskRecord(
        task_id="t1",
        func_name="f",
        status=TaskStatus.SUCCESS,
        start_time=start,
        end_time=end,
    )
    assert record.duration == 3.0


def test_to_dict_contains_expected_keys():
    record = TaskRecord(task_id="t1", func_name="func", status=TaskStatus.PENDING)
    d = record.to_dict()
    assert d["task_id"] == "t1"
    assert d["func_name"] == "func"
    assert d["status"] == "pending"
    assert d["duration"] is None


def test_task_config_defaults():
    config = TaskConfig()
    assert config.retries == 0
    assert config.delay == 0.0
    assert config.backoff == 1.0
    assert config.persist is False
