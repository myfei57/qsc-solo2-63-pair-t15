"""Immutable-by-convention domain records and their JSON codecs."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from .enums import BagState, FlightState


def _clean(raw: Any) -> Any:
    """Normalise nested containers so codecs round-trip deterministically."""

    if isinstance(raw, dict):
        return {str(key): _clean(value) for key, value in raw.items()}
    if isinstance(raw, (list, tuple)):
        return [_clean(item) for item in raw]
    return raw


@dataclass(frozen=True)
class Flight:
    id: str
    code: str
    terminal: str = "T1"
    carousel: str = ""
    chute: str = ""
    state: FlightState = FlightState.OPEN
    batch: int = 0
    schedule_generation: int = 1
    cutoff_at: str = ""
    closed_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "code": self.code,
            "terminal": self.terminal,
            "carousel": self.carousel,
            "chute": self.chute,
            "state": self.state.value,
            "batch": self.batch,
            "schedule_generation": self.schedule_generation,
            "cutoff_at": self.cutoff_at,
            "closed_at": self.closed_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Flight":
        return cls(
            id=str(raw["id"]),
            code=str(raw.get("code", "")),
            terminal=str(raw.get("terminal", "T1")),
            carousel=str(raw.get("carousel", "")),
            chute=str(raw.get("chute", "")),
            state=FlightState(raw.get("state", FlightState.OPEN.value)),
            batch=int(raw.get("batch", 0)),
            schedule_generation=int(raw.get("schedule_generation", 1)),
            cutoff_at=str(raw.get("cutoff_at", "")),
            closed_at=str(raw.get("closed_at", "")),
        )

    def evolved(self, **changes: Any) -> "Flight":
        return replace(self, **changes)


@dataclass(frozen=True)
class Bag:
    id: str
    barcode: str
    sequence: int
    epoch: int = 0
    flight_id: str = ""
    chute_id: str = ""
    state: BagState = BagState.CHECKED_IN

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "barcode": self.barcode,
            "sequence": self.sequence,
            "epoch": self.epoch,
            "flight_id": self.flight_id,
            "chute_id": self.chute_id,
            "state": self.state.value,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Bag":
        return cls(
            id=str(raw["id"]),
            barcode=str(raw.get("barcode", "")),
            sequence=int(raw.get("sequence", 0)),
            epoch=int(raw.get("epoch", 0)),
            flight_id=str(raw.get("flight_id", "")),
            chute_id=str(raw.get("chute_id", "")),
            state=BagState(raw.get("state", BagState.CHECKED_IN.value)),
        )

    def evolved(self, **changes: Any) -> "Bag":
        return replace(self, **changes)


@dataclass(frozen=True)
class Reading:
    barcode: str
    bag_id: str
    sequence: int
    epoch: int
    committed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "barcode": self.barcode,
            "bag_id": self.bag_id,
            "sequence": self.sequence,
            "epoch": self.epoch,
            "committed": self.committed,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Reading":
        return cls(
            barcode=str(raw.get("barcode", "")),
            bag_id=str(raw.get("bag_id", "")),
            sequence=int(raw.get("sequence", 0)),
            epoch=int(raw.get("epoch", 0)),
            committed=bool(raw.get("committed", False)),
        )


@dataclass(frozen=True)
class ChuteAssignment:
    flight_id: str
    chute_id: str
    generation: int = 1
    updated_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "flight_id": self.flight_id,
            "chute_id": self.chute_id,
            "generation": self.generation,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ChuteAssignment":
        return cls(
            flight_id=str(raw["flight_id"]),
            chute_id=str(raw.get("chute_id", "")),
            generation=int(raw.get("generation", 1)),
            updated_at=str(raw.get("updated_at", "")),
        )


@dataclass(frozen=True)
class SortRecord:
    bag_id: str
    flight_id: str
    chute_id: str
    sequence: int
    epoch: int
    at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "bag_id": self.bag_id,
            "flight_id": self.flight_id,
            "chute_id": self.chute_id,
            "sequence": self.sequence,
            "epoch": self.epoch,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SortRecord":
        return cls(
            bag_id=str(raw["bag_id"]),
            flight_id=str(raw.get("flight_id", "")),
            chute_id=str(raw.get("chute_id", "")),
            sequence=int(raw.get("sequence", 0)),
            epoch=int(raw.get("epoch", 0)),
            at=str(raw.get("at", "")),
        )


@dataclass(frozen=True)
class TransferCommand:
    id: str
    flight_id: str
    carousel: str
    batch: int
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "flight_id": self.flight_id,
            "carousel": self.carousel,
            "batch": self.batch,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TransferCommand":
        return cls(
            id=str(raw["id"]),
            flight_id=str(raw.get("flight_id", "")),
            carousel=str(raw.get("carousel", "")),
            batch=int(raw.get("batch", 0)),
            created_at=str(raw.get("created_at", "")),
        )


@dataclass(frozen=True)
class LoadRecord:
    bag_id: str
    carousel: str
    batch: int
    at: str
    switch_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "bag_id": self.bag_id,
            "carousel": self.carousel,
            "batch": self.batch,
            "at": self.at,
            "switch_id": self.switch_id,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "LoadRecord":
        return cls(
            bag_id=str(raw["bag_id"]),
            carousel=str(raw.get("carousel", "")),
            batch=int(raw.get("batch", 0)),
            at=str(raw.get("at", "")),
            switch_id=str(raw.get("switch_id", "")),
        )


@dataclass(frozen=True)
class Sequence:
    epoch: int = 0
    next_value: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"epoch": self.epoch, "next": self.next_value}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Sequence":
        return cls(epoch=int(raw.get("epoch", 0)), next_value=int(raw.get("next", 0)))


@dataclass(frozen=True)
class AuditEntry:
    sequence: int
    kind: str
    subject: str
    detail: dict[str, Any] = field(default_factory=dict)
    prev_digest: str = ""
    digest: str = ""
    at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "kind": self.kind,
            "subject": self.subject,
            "detail": _clean(self.detail),
            "prev_digest": self.prev_digest,
            "digest": self.digest,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AuditEntry":
        return cls(
            sequence=int(raw.get("sequence", 0)),
            kind=str(raw.get("kind", "")),
            subject=str(raw.get("subject", "")),
            detail=_clean(raw.get("detail", {})),
            prev_digest=str(raw.get("prev_digest", "")),
            digest=str(raw.get("digest", "")),
            at=str(raw.get("at", "")),
        )


#: Lifecycle of a batch switch saga.
SWITCH_RUNNING = "running"
SWITCH_COMMITTED = "committed"
SWITCH_ROLLING_BACK = "rolling_back"
SWITCH_ROLLED_BACK = "rolled_back"
SWITCH_FAILED = "failed"

#: Ordered steps of the forward cut-over; rollback compensations run in reverse.
SWITCH_STEPS: tuple[str, ...] = ("clear", "announce", "retire", "finish")


@dataclass(frozen=True)
class SwitchRecord:
    """One batch switch saga: the switch record the journal and batch state cross-check.

    Every step commits its effects and its marker in a single journal
    transaction, so a restart can ask "which markers are committed?" and be
    certain the matching effects are too. Rollback appends compensation
    commands rather than deleting history, and each compensation is marked the
    same way.
    """

    id: str
    carousel: str
    batch: int
    from_carousel: str = ""
    from_batch: int = 0
    status: str = SWITCH_RUNNING
    started_at: str = ""
    finished_at: str = ""
    #: Forward steps that committed; order follows ``SWITCH_STEPS``.
    steps_done: list[str] = field(default_factory=list)
    #: Rollback compensations that committed (rb_<step>).
    rollback_done: list[str] = field(default_factory=list)
    #: Bag ids riding the old carousel when the switch began.
    bag_ids: list[str] = field(default_factory=list)
    #: Bag state each bag had before ``clear`` marked it loaded.
    prior_bag_states: dict[str, str] = field(default_factory=dict)
    #: Transfer commands retired by ``retire`` (restored on rollback).
    retired_commands: list[dict[str, Any]] = field(default_factory=list)
    cleared: int = 0
    failure: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "carousel": self.carousel,
            "batch": self.batch,
            "from_carousel": self.from_carousel,
            "from_batch": self.from_batch,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "steps_done": list(self.steps_done),
            "rollback_done": list(self.rollback_done),
            "bag_ids": list(self.bag_ids),
            "prior_bag_states": dict(sorted(self.prior_bag_states.items())),
            "retired_commands": list(self.retired_commands),
            "cleared": self.cleared,
            "failure": self.failure,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SwitchRecord":
        return cls(
            id=str(raw["id"]),
            carousel=str(raw.get("carousel", "")),
            batch=int(raw.get("batch", 0)),
            from_carousel=str(raw.get("from_carousel", "")),
            from_batch=int(raw.get("from_batch", 0)),
            status=str(raw.get("status", SWITCH_RUNNING)),
            started_at=str(raw.get("started_at", "")),
            finished_at=str(raw.get("finished_at", "")),
            steps_done=[str(item) for item in raw.get("steps_done", [])],
            rollback_done=[str(item) for item in raw.get("rollback_done", [])],
            bag_ids=[str(item) for item in raw.get("bag_ids", [])],
            prior_bag_states={
                str(key): str(value) for key, value in raw.get("prior_bag_states", {}).items()
            },
            retired_commands=[dict(item) for item in raw.get("retired_commands", [])],
            cleared=int(raw.get("cleared", 0)),
            failure=str(raw.get("failure", "")),
        )

    def evolved(self, **changes: Any) -> "SwitchRecord":
        current = {
            "steps_done": list(self.steps_done),
            "rollback_done": list(self.rollback_done),
            "bag_ids": list(self.bag_ids),
            "prior_bag_states": dict(self.prior_bag_states),
            "retired_commands": list(self.retired_commands),
        }
        current.update(changes)
        return replace(self, **current)

    def has_step(self, step: str) -> bool:
        return step in self.steps_done

    def has_rollback(self, step: str) -> bool:
        return f"rb_{step}" in self.rollback_done
