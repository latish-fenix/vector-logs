"""A single error type that every layer raises; main.py turns it into JSON."""
from __future__ import annotations

from typing import Any


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict:
        body: dict[str, Any] = {"error": {"code": self.code, "message": self.message}}
        if self.details is not None:
            body["error"]["details"] = self.details
        return body


def bad_request(code: str, message: str, details: Any = None) -> ApiError:
    return ApiError(400, code, message, details)


def forbidden(code: str, message: str, details: Any = None) -> ApiError:
    return ApiError(403, code, message, details)


def not_found(code: str, message: str, details: Any = None) -> ApiError:
    return ApiError(404, code, message, details)


def conflict(code: str, message: str, details: Any = None) -> ApiError:
    return ApiError(409, code, message, details)


def precondition_failed(code: str, message: str, details: Any = None) -> ApiError:
    return ApiError(412, code, message, details)


def unprocessable(code: str, message: str, details: Any = None) -> ApiError:
    return ApiError(422, code, message, details)
