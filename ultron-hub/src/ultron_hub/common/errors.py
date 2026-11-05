"""Exception hierarchy. Every expected failure is an UltronHubError."""


class UltronHubError(Exception):
    """Base class for expected, user-facing errors."""


class ConfigError(UltronHubError):
    """Invalid settings.yaml or environment."""


class InvalidRequestError(UltronHubError):
    """Malformed job request (bad action name, params, job_id...)."""


class TaskNotFoundError(UltronHubError):
    """No tasks/<action>/ directory for the requested action."""


class TaskConfigError(UltronHubError):
    """task.yaml, prompt.md or output.schema.json is invalid."""


class TemplateError(UltronHubError):
    """Prompt template could not be rendered (e.g. missing param)."""
