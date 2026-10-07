"""The folded document every subsystem reads and the reducer that builds it.

The store keeps an append-only journal of committed commands. This module turns
that journal into the materialised state and knows how to apply one command,
which is what makes restart-by-replay possible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .entities import (
    AuditEntry,
    Bag,
    ChuteAssignment,
    Flight,
    LoadRecord,
    Sequence,
    SortRecord,
    SwitchRecord,
    TransferCommand,
)
from .enums import BagState
from bagsort.errors import ValidationError


@dataclass
class State:
    """Materialised view of every committed command."""

    flights: dict[str, Flight] = field(default_factory=dict)
    bags: dict[str, Bag] = field(default_factory=dict)
    mappings: dict[str, str] = field(default_factory=dict)
    assignments: dict[str, ChuteAssignment] = field(default_factory=dict)
    sort_records: list[SortRecord] = field(default_factory=list)
    transfer_commands: list[TransferCommand] = field(default_factory=list)
    load_records: list[LoadRecord] = field(default_factory=list)
    audit: list[AuditEntry] = field(default_factory=list)
    quota_limits: dict[str, int] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)
    sequence: Sequence = field(default_factory=Sequence)
    recheck_queue: list[str] = field(default_factory=list)
    belt_carousel: str = ""
    belt_batch: int = 0
    belt_on_carousel: list[str] = field(default_factory=list)
    switches: dict[str, SwitchRecord] = field(default_factory=dict)

    def copy(self) -> "State":
        return State(
            flights=dict(self.flights),
            bags=dict(self.bags),
            mappings=dict(self.mappings),
            assignments=dict(self.assignments),
            sort_records=list(self.sort_records),
            transfer_commands=list(self.transfer_commands),
            load_records=list(self.load_records),
            audit=list(self.audit),
            quota_limits=dict(self.quota_limits),
            counters=dict(self.counters),
            sequence=self.sequence,
            recheck_queue=list(self.recheck_queue),
            belt_carousel=self.belt_carousel,
            belt_batch=self.belt_batch,
            belt_on_carousel=list(self.belt_on_carousel),
            switches=dict(self.switches),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "flights": {key: value.as_dict() for key, value in sorted(self.flights.items())},
            "bags": {key: value.as_dict() for key, value in sorted(self.bags.items())},
            "mappings": dict(sorted(self.mappings.items())),
            "assignments": {
                key: value.as_dict() for key, value in sorted(self.assignments.items())
            },
            "sort_records": [record.as_dict() for record in self.sort_records],
            "transfer_commands": [command.as_dict() for command in self.transfer_commands],
            "load_records": [record.as_dict() for record in self.load_records],
            "audit": [entry.as_dict() for entry in self.audit],
            "quota_limits": dict(sorted(self.quota_limits.items())),
            "counters": dict(sorted(self.counters.items())),
            "sequence": self.sequence.as_dict(),
            "recheck_queue": list(self.recheck_queue),
            "belt": {
                "carousel": self.belt_carousel,
                "batch": self.belt_batch,
                "on_carousel": list(self.belt_on_carousel),
            },
            "switches": {
                key: value.as_dict() for key, value in sorted(self.switches.items())
            },
        }

def apply_entry(state: State, kind: str, payload: dict[str, Any]) -> None:
    """Apply one journal command to ``state`` in place."""

    if kind == "flight.put":
        flight = Flight.from_dict(payload["flight"])
        state.flights[flight.id] = flight
    elif kind == "flight.remove":
        state.flights.pop(str(payload["flight_id"]), None)
        state.mappings = {
            bag_id: flight_id
            for bag_id, flight_id in state.mappings.items()
            if flight_id != str(payload["flight_id"])
        }
    elif kind == "chute.assign":
        assignment = ChuteAssignment.from_dict(payload)
        state.assignments[assignment.flight_id] = assignment
    elif kind == "bag.put":
        bag = Bag.from_dict(payload["bag"])
        state.bags[bag.id] = bag
    elif kind == "bag.remove":
        state.bags.pop(str(payload["bag_id"]), None)
        state.mappings.pop(str(payload["bag_id"]), None)
    elif kind == "mapping.put":
        state.mappings[str(payload["bag_id"])] = str(payload["flight_id"])
    elif kind == "sequence.put":
        state.sequence = Sequence.from_dict(payload)
    elif kind == "sort.record":
        state.sort_records.append(SortRecord.from_dict(payload["record"]))
    elif kind == "transfer.append":
        state.transfer_commands.append(TransferCommand.from_dict(payload["command"]))
    elif kind == "transfer.clear_before":
        floor = int(payload["batch"])
        state.transfer_commands = [
            command for command in state.transfer_commands if command.batch >= floor
        ]
    elif kind == "load.append":
        state.load_records.append(LoadRecord.from_dict(payload["record"]))
    elif kind == "audit.append":
        state.audit.append(AuditEntry.from_dict(payload["entry"]))
    elif kind == "quota.set":
        state.quota_limits[str(payload["flight_id"])] = int(payload["limit"])
    elif kind == "quota.adjust":
        flight_id = str(payload["flight_id"])
        base = state.quota_limits.get(flight_id, 0)
        state.quota_limits[flight_id] = base + int(payload["delta"])
    elif kind == "counter.add":
        name = str(payload["name"])
        state.counters[name] = state.counters.get(name, 0) + int(payload["delta"])
    elif kind == "recheck.enqueue":
        bag_id = str(payload["bag_id"])
        if bag_id not in state.recheck_queue:
            state.recheck_queue.append(bag_id)
    elif kind == "recheck.dequeue":
        bag_id = str(payload["bag_id"])
        state.recheck_queue = [item for item in state.recheck_queue if item != bag_id]
    elif kind == "recheck.clear":
        state.recheck_queue = []
    elif kind == "belt.set":
        state.belt_carousel = str(payload.get("carousel", ""))
        state.belt_batch = int(payload.get("batch", 0))
    elif kind == "belt.place":
        bag_id = str(payload["bag_id"])
        if bag_id not in state.belt_on_carousel:
            state.belt_on_carousel.append(bag_id)
    elif kind == "belt.clear":
        state.belt_on_carousel = []
        state.belt_carousel = str(payload.get("carousel", state.belt_carousel))
        state.belt_batch = int(payload.get("batch", state.belt_batch))
    elif kind == "belt.restore":
        state.belt_on_carousel = [
            str(item) for item in payload.get("on_carousel", ())
        ]
        state.belt_carousel = str(payload.get("carousel", state.belt_carousel))
        state.belt_batch = int(payload.get("batch", state.belt_batch))
    elif kind == "bag.setstate":
        bag_id = str(payload["bag_id"])
        existing = state.bags.get(bag_id)
        if existing is not None:
            state.bags[bag_id] = existing.evolved(state=BagState(payload["state"]))
    elif kind == "load.remove":
        switch_id = str(payload.get("switch_id", ""))
        if switch_id:
            state.load_records = [
                record
                for record in state.load_records
                if record.switch_id != switch_id
            ]
        else:
            state.load_records = [
                record
                for record in state.load_records
                if record.bag_id != str(payload["bag_id"])
            ]
    elif kind == "load.append":
        state.load_records.append(LoadRecord.from_dict(payload["record"]))
    elif kind == "transfer.rest":
        restored = [TransferCommand.from_dict(item) for item in payload.get("commands", ())]
        known = {command.id for command in state.transfer_commands}
        for command in restored:
            if command.id not in known:
                state.transfer_commands.append(command)
                known.add(command.id)
    elif kind == "switch.begin":
        record = SwitchRecord.from_dict(payload["switch"])
        state.switches[record.id] = record
    elif kind == "switch.step":
        switch_id = str(payload["switch_id"])
        switch = state.switches[switch_id]
        step = str(payload["step"])
        steps_done = list(switch.steps_done)
        if step not in steps_done:
            steps_done.append(step)
        changes_step: dict[str, Any] = {"steps_done": steps_done}
        # Carry the rollback evidence the step captured at run time, so a
        # replayed log after restart still knows what to restore.
        if "cleared" in payload:
            changes_step["cleared"] = int(payload["cleared"])
        if "prior_bag_states" in payload:
            changes_step["prior_bag_states"] = {
                str(key): str(value)
                for key, value in payload["prior_bag_states"].items()
            }
        if "retired_commands" in payload:
            changes_step["retired_commands"] = [
                dict(item) for item in payload["retired_commands"]
            ]
        state.switches[switch_id] = switch.evolved(**changes_step)
    elif kind == "switch.fail":
        # A failure note records what went wrong; the status stays whatever
        # the step markers prove (running), so recovery remains possible.
        switch_id = str(payload["switch_id"])
        switch = state.switches[switch_id]
        note = f"{payload.get('step', '')}: {payload.get('error', '')}".strip(": ")
        state.switches[switch_id] = switch.evolved(failure=note)
    elif kind == "switch.status":
        switch_id = str(payload["switch_id"])
        switch = state.switches[switch_id]
        changes: dict[str, Any] = {"status": str(payload["status"])}
        if "finished_at" in payload:
            changes["finished_at"] = str(payload["finished_at"])
        state.switches[switch_id] = switch.evolved(**changes)
    elif kind == "switch.rbstep":
        switch_id = str(payload["switch_id"])
        switch = state.switches[switch_id]
        step = f"rb_{payload['step']}"
        rollback_done = list(switch.rollback_done)
        if step not in rollback_done:
            rollback_done.append(step)
        switch = switch.evolved(rollback_done=rollback_done)
        if "status" in payload:
            switch = switch.evolved(
                status=str(payload["status"]),
                finished_at=str(payload.get("finished_at", switch.finished_at)),
            )
        state.switches[switch_id] = switch
    else:  # pragma: no cover - guards against a typo in a new command kind
        raise ValidationError(f"unknown journal command {kind!r}")


def fold(entries: list[tuple[str, dict[str, Any]]]) -> State:
    """Rebuild a :class:`State` from a sequence of ``(kind, payload)`` pairs."""

    state = State()
    for kind, payload in entries:
        apply_entry(state, kind, payload)
    return state
