"""Carousel and batch transfer.

Switching a batch is a saga (see :mod:`bagsort.belt.switching`); this module
owns the carousel state and the session scoped primitives the saga composes.
The important ordering rule never changes: the current carousel is cleared
*before* the next batch is announced, and the previous batch's transfer
commands are retired *after* the announcement. Doing either in the other order
is how bags ride across batches and how a stale command re-delivers a bag to
the carousel it already left.
"""

from __future__ import annotations

from bagsort.clock import Clock
from bagsort.errors import ConflictError, OrderingError
from bagsort.ids import IdFactory
from bagsort.model.entities import (
    Bag,
    LoadRecord,
    SWITCH_ROLLING_BACK,
    SWITCH_RUNNING,
    TransferCommand,
)
from bagsort.model.enums import BagState
from bagsort.model.validate import validate_transfer
from bagsort.store.store import Session, Store


class Belt:
    """The outbound carousel and the transfer commands that feed it."""

    def __init__(self, store: Store, clock: Clock, ids: IdFactory) -> None:
        self._store = store
        self._clock = clock
        self._ids = ids

    def carousel(self) -> str:
        return self._store.state().belt_carousel

    def batch(self) -> int:
        return self._store.state().belt_batch

    def describe(self) -> dict[str, object]:
        return {
            "carousel": self.carousel(),
            "batch": self.batch(),
            "on_carousel": len(self._store.state().belt_on_carousel),
        }

    def set(self, carousel: str, batch: int) -> dict[str, object]:
        if batch < 0:
            raise OrderingError("batch number must not be negative")
        self.ensure_idle()
        with self._store.session() as session:
            self.apply_set(session, carousel.strip(), batch)
        return self.describe()

    def place(self, bag: Bag) -> int:
        self.ensure_idle()
        with self._store.session() as session:
            self.apply_place(session, bag)
        return len(self._store.state().belt_on_carousel)

    def place_batch(self, bags: list[Bag]) -> int:
        self.ensure_idle()
        for bag in bags:
            with self._store.session() as session:
                self.apply_place(session, bag)
        return len(self._store.state().belt_on_carousel)

    def on_carousel(self) -> list[Bag]:
        state = self._store.state()
        return [state.bags[bag_id] for bag_id in state.belt_on_carousel if bag_id in state.bags]

    def loads(self) -> list[LoadRecord]:
        return list(self._store.state().load_records)

    def rotate(self) -> int:
        """Retire transfer commands older than the running batch."""

        if not self.carousel():
            return 0
        return self.discard_stale(self.batch())

    def append_transfer(self, flight_id: str, carousel: str, batch: int) -> TransferCommand:
        self.ensure_idle()
        command = TransferCommand(
            id=self._ids.next(),
            flight_id=flight_id.strip(),
            carousel=carousel.strip(),
            batch=batch,
            created_at=self._clock.now_iso(),
        )
        validate_transfer(command)
        with self._store.session() as session:
            session.state.transfer_commands.append(command)
            session.record(
                "transfer.append", {"command": command.as_dict()}, self._clock.now_iso()
            )
        return command

    def transfers(self) -> list[TransferCommand]:
        return list(self._store.state().transfer_commands)

    def replay_transfers(self) -> list[TransferCommand]:
        """Commands that still belong to the batch currently on the carousel."""

        batch = self.batch()
        return [
            command
            for command in self._store.state().transfer_commands
            if command.batch == batch
        ]

    def discard_stale(self, floor: int) -> int:
        """Drop every transfer command older than ``floor``; returns how many went."""

        doomed = [
            command for command in self._store.state().transfer_commands if command.batch < floor
        ]
        if not doomed:
            return 0
        with self._store.session() as session:
            self.apply_discard_before(session, floor)
        return len(doomed)

    # -- saga support ------------------------------------------------------

    def ensure_idle(self) -> None:
        """Reject belt mutations while a switch is unresolved.

        Feeding the carousel mid cut-over is how bags end up belonging to
        neither batch; callers must finish (resume) or undo (rollback) the
        open switch first.
        """

        active = self.unresolved_switch()
        if active is not None:
            raise ConflictError(
                f"batch switch {active.id} is {active.status}; resume or roll it "
                "back before touching the carousel"
            )

    def unresolved_switch(self):
        """The open switch saga, if any (running or rolling back)."""

        switches = self._store.state().switches
        pending = [
            record
            for record in switches.values()
            if record.status in (SWITCH_RUNNING, SWITCH_ROLLING_BACK)
        ]
        pending.sort(key=lambda record: record.started_at)
        return pending[-1] if pending else None

    def apply_set(self, session: Session, carousel: str, batch: int) -> None:
        """Announce the carousel/batch inside an existing session."""

        session.state.belt_carousel = carousel
        session.state.belt_batch = batch
        session.record(
            "belt.set",
            {"carousel": carousel, "batch": batch},
            self._clock.now_iso(),
        )

    def apply_place(self, session: Session, bag: Bag) -> None:
        if bag.id not in session.state.belt_on_carousel:
            session.state.belt_on_carousel.append(bag.id)
        session.record("belt.place", {"bag_id": bag.id}, self._clock.now_iso())

    def apply_clear(
        self,
        session: Session,
        carousel: str,
        batch: int,
        switch_id: str = "",
    ) -> tuple[int, dict[str, str]]:
        """Clear the carousel, loading every bag riding it, inside one session.

        Returns how many bags were cleared and the state each one had before
        this clear marked it LOADED, so a rollback can put them back exactly.
        """

        pending = list(session.state.belt_on_carousel)
        prior: dict[str, str] = {}
        at = self._clock.now_iso()
        for bag_id in pending:
            bag = session.state.bags.get(bag_id)
            if bag is not None:
                prior[bag_id] = bag.state.value
                if bag.state is not BagState.LOADED:
                    updated = bag.evolved(state=BagState.LOADED)
                    session.state.bags[bag_id] = updated
                    session.record("bag.put", {"bag": updated.as_dict()}, at)
            else:  # pragma: no cover - a placed bag always exists in state
                prior[bag_id] = BagState.LOADED.value
            record = LoadRecord(
                bag_id=bag_id,
                carousel=carousel,
                batch=batch,
                at=at,
                switch_id=switch_id,
            )
            session.state.load_records.append(record)
            session.record("load.append", {"record": record.as_dict()}, at)
        session.state.belt_on_carousel = []
        session.record("belt.clear", {"carousel": carousel, "batch": batch}, at)
        session.record("counter.add", {"name": "belt.loaded", "delta": len(pending)}, at)
        return len(pending), prior

    def apply_discard_before(
        self, session: Session, floor: int
    ) -> list[TransferCommand]:
        """Retire commands older than ``floor`` inside one session.

        The retired commands are returned whole because the switch record
        carries them as the snapshot a rollback restores from.
        """

        doomed = [
            command for command in session.state.transfer_commands if command.batch < floor
        ]
        if doomed:
            session.state.transfer_commands = [
                command
                for command in session.state.transfer_commands
                if command.batch >= floor
            ]
            session.record(
                "transfer.clear_before", {"batch": floor}, self._clock.now_iso()
            )
        return doomed

    # -- rollback primitives (compensation commands, never deletions) ------

    def apply_restore_commands(
        self, session: Session, commands: list[TransferCommand]
    ) -> int:
        """Re-append retired commands inside one session; deduped and marked."""

        known = {command.id for command in session.state.transfer_commands}
        restored = [command for command in commands if command.id not in known]
        session.state.transfer_commands.extend(restored)
        if restored:
            session.record(
                "transfer.rest",
                {"commands": [command.as_dict() for command in restored]},
                self._clock.now_iso(),
            )
        return len(restored)
