from fastapi_taskflow import TaskManager


def test_task_decorator_registers_config():
    tm = TaskManager()

    @tm.task(retries=3, delay=1.0, backoff=2.0)
    def my_task():
        pass

    config = tm.registry.get_config(my_task)
    assert config is not None
    assert config.retries == 3
    assert config.delay == 1.0
    assert config.backoff == 2.0


def test_task_decorator_preserves_callable():
    tm = TaskManager()

    @tm.task()
    def double(x: int) -> int:
        return x * 2

    assert double(5) == 10


def test_task_decorator_sets_default_name():
    tm = TaskManager()

    @tm.task()
    def my_func():
        pass

    config = tm.registry.get_config(my_func)
    assert config.name == "my_func"


def test_task_decorator_custom_name():
    tm = TaskManager()

    @tm.task(name="custom_name")
    def my_func():
        pass

    config = tm.registry.get_config(my_func)
    assert config.name == "custom_name"


def test_unregistered_func_returns_none():
    tm = TaskManager()

    def plain():
        pass

    assert tm.registry.get_config(plain) is None


def test_persist_flag():
    tm = TaskManager()

    @tm.task(persist=True)
    def my_task():
        pass

    config = tm.registry.get_config(my_task)
    assert config.persist is True
