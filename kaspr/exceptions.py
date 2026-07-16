"""Kaspr exceptions."""

__all__ = [
    "KasprError",
    "KasprPredicate",
    "Skip",
]


class KasprError(Exception):
    """Base-class for all Kaspr exceptions."""


class KasprPredicate(KasprError):
    """Base-class for semi-predicates such as :exc:`Skip`."""


class Skip(KasprPredicate):
    """Raised in stream processors to skip processing of an event."""


class KasprProcessingError(KasprError):
    """Raised when an error occurs during processing."""

    operation: str = None
    cause: Exception = None

    def __init__(self, message: str, cause: Exception = None, operation: str = None):
        super().__init__(message)
        self.operation = operation
        self.cause = cause

    def __str__(self):
        return f"{self.args[0]}: {self.cause}" if self.cause else self.args[0]

    def __repr__(self):
        return f"{self.__class__.__name__}({self.args[0]!r}, {self.cause!r})"

    def to_dict(self, include_cause_object: bool = False):
        payload = {
            "operation": self.operation,
            "message": self.args[0] if self.args else None,
            # Keep `cause` as a string for backwards compatibility.
            "cause": str(self.cause) if self.cause else None,
        }

        if include_cause_object:
            payload["cause_object"] = self.cause

        if self.cause:
            for attr_name in ("status_code", "code", "details", "message"):
                if hasattr(self.cause, attr_name):
                    payload[attr_name] = getattr(self.cause, attr_name)

            to_response = getattr(self.cause, "to_response", None)
            if callable(to_response):
                try:
                    payload["cause_response"] = to_response()
                except Exception:
                    # The original cause is still available via `cause_object`.
                    payload["cause_response"] = None

        return payload
