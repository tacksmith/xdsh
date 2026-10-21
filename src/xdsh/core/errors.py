"""Runner-neutral failures."""


class RunnerError(Exception):
    """Base class for failures owned by a runner boundary."""


class RunnerConfigurationError(RunnerError):
    """Runner configuration is invalid or unsafe."""


class RunnerClosedError(RunnerError):
    """The runner has already been closed."""


class SessionBusyError(RunnerError):
    """A session already has an active task."""


class SessionConfigurationError(RunnerError):
    """A stable session was reused with incompatible configuration."""

