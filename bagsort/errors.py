"""Exception hierarchy shared by every subsystem."""

from __future__ import annotations


class ControlError(Exception):
    """Base class for every error raised by the control service."""

    code = "control_error"

    def as_dict(self) -> dict[str, str]:
        return {"error": str(self), "code": self.code}


class ValidationError(ControlError):
    """An entity or payload failed a structural check."""

    code = "validation_error"


class NotFoundError(ControlError):
    """A requested entity does not exist."""

    code = "not_found"


class ConflictError(ControlError):
    """The request contradicts the committed state."""

    code = "conflict"


class OrderingError(ControlError):
    """A step was attempted out of the order the plant allows."""

    code = "ordering_error"


class SwitchInterrupted(ControlError):
    """A drill-injected fault stopped a switch after a committed step.

    Every effect before the fault is already durable; the stranded switch is
    recovered from the journal and settled with continue or rollback.
    """

    code = "switch_interrupted"

    def __init__(self, switch_id: str, steps_done: int) -> None:
        super().__init__(f"switch {switch_id} interrupted after {steps_done} step(s)")
        self.switch_id = switch_id
        self.steps_done = steps_done


class StaleError(ControlError):
    """A generation, watermark or schedule reference is out of date."""

    code = "stale_reference"


class QuotaError(ControlError):
    """A quota or capacity bound rejected the request."""

    code = "quota_exceeded"
