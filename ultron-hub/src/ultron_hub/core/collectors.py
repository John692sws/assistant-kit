"""Known data collectors a task can request with `collect: <name>` in task.yaml.

A collector is plain code that runs before Claude and returns JSON-able data,
inserted in the prompt as the `{metrics}` placeholder. Only collectors listed
here can run: task.yaml never names a command or a module.
"""

from typing import Any, Callable

from ultron_hub.docs.collect import collect_docs
from ultron_hub.health.collect import collect as collect_health
from ultron_hub.montage.collect import collect_montage

COLLECTORS: dict[str, Callable[..., Any]] = {
    "health": collect_health,
    "montage": collect_montage,
    "docs": collect_docs,
}
# Collectors that receive the job's params (a montage or docs id); the others take no argument.
PARAM_COLLECTORS = frozenset({"montage", "docs"})
