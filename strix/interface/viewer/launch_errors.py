"""The error type shared by the viewer launch pipeline (error-code table).

Every validation and launch failure is a :class:`LaunchError` carrying a stable
machine code, a human message, and the HTTP status the server should return.
Keeping the mapping on the exception lets each layer raise without knowing how
the server will serialise it.
"""

from __future__ import annotations

from typing import Any


class LaunchError(Exception):
    """A launch-pipeline failure with a stable code, message and HTTP status.

    ``extra`` carries optional fields the client needs (for example the active
    run name on ``scan_active``); it is merged into the JSON payload.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra: dict[str, Any] = dict(extra) if extra else {}

    def to_payload(self) -> dict[str, Any]:
        """Serialise to the JSON error envelope the API returns."""
        return {"error": self.code, "message": self.message, **self.extra}
