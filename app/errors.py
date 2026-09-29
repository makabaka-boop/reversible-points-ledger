"""Domain errors mapped to HTTP responses in main.py."""

from __future__ import annotations


class LedgerError(Exception):
    status_code = 409


class NotFound(LedgerError):
    status_code = 404


class BadRequest(LedgerError):
    status_code = 400


class Unauthorized(LedgerError):
    status_code = 401


class Conflict(LedgerError):
    status_code = 409


class Unavailable(LedgerError):
    """Raised when the database lock cannot be acquired."""

    status_code = 503
