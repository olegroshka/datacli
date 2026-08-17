"""Shared scheduling substrate for datacli.

The package owns command admission, durable job definitions, execution
history, locks, and the backend port.  Operating-system adapters only decide
when a runner is launched.
"""

from .model import SCHEMA_VERSION, CommandResult, JobSpec, RunRecord

__all__ = ["SCHEMA_VERSION", "CommandResult", "JobSpec", "RunRecord"]
