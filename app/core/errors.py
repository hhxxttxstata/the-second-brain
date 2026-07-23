"""Application-wide error definitions."""

from __future__ import annotations

from fastapi import HTTPException, status


class AppError(Exception):
    """Base application error."""

    def __init__(self, message: str, code: str | None = None) -> None:
        self.message = message
        self.code = code
        super().__init__(message)


class NotFoundError(AppError):
    """Resource not found."""

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(
            message=f"{resource} not found: {resource_id}",
            code="not_found",
        )


class ValidationError(AppError):
    """Validation failed."""

    def __init__(self, message: str) -> None:
        super().__init__(message=message, code="validation_error")


class AuthorizationError(AppError):
    """Not authorized."""

    def __init__(self, message: str = "Not authorized") -> None:
        super().__init__(message=message, code="unauthorized")


def not_found(resource: str, resource_id: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "not_found", "message": f"{resource} not found: {resource_id}"},
    )


def bad_request(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"code": "bad_request", "message": message},
    )
