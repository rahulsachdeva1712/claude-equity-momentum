class DhanError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, payload: dict | None = None):
        super().__init__(message)
        self.status = status
        self.payload = payload or {}


class DhanAuthError(DhanError):
    pass


class DhanRejected(DhanError):
    """Order accepted by transport but rejected by Dhan/exchange."""


class DhanUnavailable(DhanError):
    """5xx or network issue; retry-eligible at the caller's discretion."""
