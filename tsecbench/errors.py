"""Business errors returned by the public Challenges API."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class APIError(Exception):
    status_code: int
    code: str
    message: str
    detail: dict[str, Any]

    def __init__(self, status_code: int, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.detail = {} if detail is None else detail

    def as_response(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": self.detail}


def task_not_found() -> APIError:
    return APIError(404, "task_not_found", "Task not found")


def challenge_not_found() -> APIError:
    return APIError(404, "challenge_not_found", "Challenge not found")


def invalid_state(message: str = "Task is no longer active") -> APIError:
    return APIError(409, "invalid_state", message)


def duplicate() -> APIError:
    return APIError(409, "duplicate", "Flag has already been submitted")


def resource_unavailable(message: str = "Challenge resources are unavailable") -> APIError:
    return APIError(503, "resource_unavailable", message)


def internal_error() -> APIError:
    return APIError(500, "internal_error", "Internal server error")
