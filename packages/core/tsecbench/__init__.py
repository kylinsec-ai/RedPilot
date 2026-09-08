"""TSecBench platform package."""

from .config import Settings
from .models import ChallengeDefinition, FlagDefinition, TaskDefinition


def create_app(*args, **kwargs):
    """Build the API application without importing FastAPI for core use."""
    from .api import create_app as factory

    return factory(*args, **kwargs)


__all__ = [
    "ChallengeDefinition",
    "FlagDefinition",
    "Settings",
    "TaskDefinition",
    "create_app",
]
