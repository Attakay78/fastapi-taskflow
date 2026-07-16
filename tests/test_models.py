from datetime import datetime, timezone

from fastapi_taskflow.models import AuditEntry, TaskConfig, TaskRecord, TaskStatus


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


def test_to_dict_timestamps_carry_utc_offset_when_naive():
    # Naive datetimes (the historical convention for created_at) must still
    # serialize with an explicit UTC offset, since an offset-less ISO string
    # is parsed as local time by JS `Date` and most other clients.
    naive = datetime(2024, 1, 1, 12, 0, 0)
    record = TaskRecord(
        task_id="t1",
        func_name="func",
        status=TaskStatus.SUCCESS,
        created_at=naive,
        start_time=naive,
        end_time=naive,
    )
    d = record.to_dict()
    assert d["created_at"] == "2024-01-01T12:00:00+00:00"
    assert d["start_time"] == "2024-01-01T12:00:00+00:00"
    assert d["end_time"] == "2024-01-01T12:00:00+00:00"


def test_to_dict_timestamps_preserve_aware_utc_offset():
    aware = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    record = TaskRecord(
        task_id="t1",
        func_name="func",
        status=TaskStatus.SUCCESS,
        created_at=aware,
    )
    d = record.to_dict()
    assert d["created_at"] == "2024-01-01T12:00:00+00:00"


def test_audit_entry_to_dict_timestamp_carries_utc_offset_when_naive():
    entry = AuditEntry(
        entry_id="e1",
        action="retry",
        task_id="t1",
        actor="anonymous",
        timestamp=datetime(2024, 1, 1, 12, 0, 0),
    )
    assert entry.to_dict()["timestamp"] == "2024-01-01T12:00:00+00:00"


def test_task_config_defaults():
    config = TaskConfig()
    assert config.retries == 0
    assert config.delay == 0.0
    assert config.backoff == 1.0
    assert config.persist is False
