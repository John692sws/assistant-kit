from pathlib import Path

import pytest
import yaml

TASKS = sorted((Path(__file__).resolve().parent.parent / "tasks").glob("*/task.yaml"))


def test_tasks_found():
    assert TASKS


@pytest.mark.parametrize("path", TASKS, ids=lambda p: p.parent.name)
def test_task_yaml_loads(path):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and data.get("name") == path.parent.name
