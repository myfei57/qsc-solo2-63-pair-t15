"""Crash safe batch switching.

A cut-over is a four step saga:

1. ``clear``    - every bag on the old carousel is loaded and accounted for.
2. ``announce`` - the new carousel/batch becomes the one the belt points at.
3. ``retire``   - the previous batch's transfer commands are stood down.
4. ``finish``   - the switch record is marked committed.

Each step, *including its marker on the switch record*, commits in one journal
transaction. So on restart the coordinator only has to ask which markers are
present: a marker that committed guarantees the step's effects committed with
it, and a missing marker guarantees nothing of that step is visible. Resume
replays from the journal (the materialised state already is that replay) and
performs only the missing steps; rollback plays compensations in reverse
(``rb_retire``, ``rb_announce``, ``rb_clear``), appending corrective commands
rather than erasing history.

The accounting rule for the bags caught on the carousel:

* **resume** keeps the ``clear`` outcome: the stranded bags are loaded against
  the *old* batch and ride no further. The stuck side is the new batch, which
  never finished coming up.
* **rollback** reverses ``clear``: the load records are withdrawn, the bags
  return to their prior state and are put back on the *old* carousel. The stuck
  side is the new batch, whose announcement is undone.

Either way exactly one batch is valid at the end: the journal marker order
makes a state in which both the old and new carousel announcements are live
unreachable.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from bagsort.belt.belt import Belt
from bagsort.clock import Clock
from bagsort.errors import ConflictError, OrderingError, SwitchInterrupted
from bagsort.ids import IdFactory
from bagsort.model.entities import (
    SWITCH_COMMITTED,
    SWITCH_ROLLED_BACK,
    SWITCH_ROLLING_BACK,
    SWITCH_RUNNING,
    SWITCH_STEPS,
    SwitchRecord,
    TransferCommand,
)
from bagsort.model.enums import BagState
from bagsort.model.validate import require_text
from bagsort.store.store import Session, Store

#: Compensation for step N, ordered so rollback runs finish..clear.
ROLLBACK_STEPS = tuple(reversed(SWITCH_STEPS[:-1]))


class InjectedFault(RuntimeError):
    """Test hook standing in for a process dying mid saga."""


@dataclass(frozen=True)
class SwitchOutcome:
    """What a begin/resume/rollback call ended up doing."""

    switch_id: str
    status: str
    cleared: int
    restored: int
    carousel: str
    batch: int
    from_carousel: str
    from_batch: int
    #: Which side of the cut-over was left stranded by a midpoint stop.
    stranded_end: str
    #: Bags stranded when the saga stopped midpoint (empty on a clean finish).
    stranded_bag_ids: list[str]
    steps_done: list[str]
    rollback_done: list[str]
    interrupted: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "switch_id": self.switch_id,
            "status": self.status,
            "cleared": self.cleared,
            "restored": self.restored,
            "carousel": self.carousel,
            "batch": self.batch,
            "from_carousel": self.from_carousel,
            "from_batch": self.from_batch,
            "stranded_end": self.stranded_end,
            "stranded_bags": list(self.stranded_bag_ids),
            "steps_done": list(self.steps_done),
            "rollback_done": list(self.rollback_done),
            "interrupted": self.interrupted,
        }


class SwitchCoordinator:
    """Starts, resumes and rolls back batch switches against the journal."""

    def __init__(
        self,
        store: Store,
        belt: Belt,
        clock: Clock,
        ids: IdFactory | None = None,
        listener=None,
    ) -> None:
        self._store = store
        self._belt = belt
        self._clock = clock
        self._ids = ids or IdFactory("sw")
        # Called as ``listener(event_name, **payload)`` whenever the saga
        # changes phase (began, interrupted, committed, rolling back/back).
        # Audit subscribes so the decision trail survives a midpoint crash.
        self._listener = listener
        # Test hook: die after committing the named forward step, as a real
        # process would - nothing after the commit runs, including audit.
        self.crash_after: str = ""
        # Test hook: raise inside the named step's session, before its commit.
        self.fail_in: str = ""
        # Only one saga may run at a time; the console serves worker threads.
        self._lock = threading.RLock()

    @contextmanager
    def _guarded(self):
        # A blocking lock is deliberate: the second operator call waits for
        # the first to settle instead of interleaving steps on one carousel.
        with self._lock:
            yield

    def _emit(self, event: str, **payload: Any) -> None:
        if self._listener is not None:
            self._listener(event, **payload)

    # -- reads -------------------------------------------------------------

    def records(self) -> list[SwitchRecord]:
        return [
            self._store.state().switches[key]
            for key in sorted(self._store.state().switches)
        ]

    def get(self, switch_id: str) -> SwitchRecord:
        record = self._store.state().switches.get(switch_id)
        if record is None:
            raise ConflictError(f"no batch switch {switch_id!r}")
        return record

    def active(self) -> SwitchRecord | None:
        """The unresolved switch an operator must make a decision about."""

        return self._belt.unresolved_switch()

    def pending(self) -> list[SwitchRecord]:
        """Every switch not yet resolved (running or rolling back)."""

        return [
            record
            for record in self.records()
            if record.status in (SWITCH_RUNNING, SWITCH_ROLLING_BACK)
        ]

    def describe(self, record: SwitchRecord) -> dict[str, Any]:
        """Operator facing summary, naming the side and the bags at stake."""

        stranded_end, stranded_bags = self._stranded(record)
        resume_loads, rollback_loads = self._loaded_bag_impact(record)
        return {
            "switch_id": record.id,
            "status": record.status,
            "from": {"carousel": record.from_carousel, "batch": record.from_batch},
            "to": {"carousel": record.carousel, "batch": record.batch},
            "steps_done": list(record.steps_done),
            "rollback_done": list(record.rollback_done),
            "cleared": record.cleared,
            "failure": record.failure,
            "stranded_end": stranded_end,
            "stranded_bags": list(stranded_bags),
            # The bags the midpoint clear already loaded. Resume keeps those
            # load records (bags stay loaded against the old batch); rollback
            # withdraws them and puts the bags back on the old carousel.
            "loaded_bags": list(record.bag_ids),
            "resume_keeps_loaded": list(resume_loads),
            "rollback_unloads": list(rollback_loads),
            "resumable": record.status == SWITCH_RUNNING,
            "rollback_available": record.status
            in (SWITCH_RUNNING, SWITCH_ROLLING_BACK),
        }

    # -- forward -----------------------------------------------------------

    def begin(self, carousel: str, batch: int) -> SwitchOutcome:
        """Start a cut-over and drive it as far as it will go.

        Raises :class:`SwitchInterrupted` if it stopped midpoint; the switch
        stays in the journal, waiting for :meth:`resume` or :meth:`rollback`.
        """

        with self._guarded():
            carousel = require_text(carousel, "carousel")
            if batch < 0:
                raise OrderingError("batch number must not be negative")
            existing = self.active()
            if existing is not None:
                raise ConflictError(
                    f"batch switch {existing.id} is already {existing.status}; "
                    "resume or roll it back before starting another"
                )
            state = self._store.state()
            switch_id = self._next_id()
            at = self._clock.now_iso()
            record = SwitchRecord(
                id=switch_id,
                carousel=carousel,
                batch=batch,
                from_carousel=state.belt_carousel,
                from_batch=state.belt_batch,
                status=SWITCH_RUNNING,
                started_at=at,
                bag_ids=list(state.belt_on_carousel),
                cleared=len(state.belt_on_carousel),
            )
            with self._store.session() as session:
                session.state.switches[switch_id] = record
                session.record("switch.begin", {"switch": record.as_dict()}, at)
            self._emit(
                "began",
                switch_id=switch_id,
                carousel=carousel,
                batch=batch,
                from_carousel=record.from_carousel,
                from_batch=record.from_batch,
                cleared=record.cleared,
            )
            return self._drive(switch_id)

    def resume(self, switch_id: str = "") -> SwitchOutcome:
        """Finish an interrupted switch, committing only steps not yet done."""

        with self._guarded():
            record = self._require(switch_id)
            if record.status != SWITCH_RUNNING:
                raise ConflictError(
                    f"switch {record.id} is {record.status}, not running; "
                    "only a running switch can be resumed"
                )
            return self._drive(record.id)

    # -- rollback ----------------------------------------------------------

    def rollback(self, switch_id: str = "") -> SwitchOutcome:
        """Undo a cut-over back to the pre-switch batch, in reverse step order."""

        with self._guarded():
            record = self._require(switch_id)
            if record.status == SWITCH_COMMITTED:
                raise ConflictError(
                    f"switch {record.id} is committed; a finished cut-over cannot "
                    "be rolled back"
                )
            if record.status == SWITCH_ROLLED_BACK:
                return self._outcome(record)
            if record.status not in (SWITCH_RUNNING, SWITCH_ROLLING_BACK):
                raise ConflictError(
                    f"switch {record.id} cannot be rolled back from {record.status!r}"
                )

            for step in ROLLBACK_STEPS:
                record = self.get(record.id)
                if record.has_step(step) and not record.has_rollback(step):
                    self._compensate(record, step)
            final = self.get(record.id)
            self._emit(
                "rolled_back",
                switch_id=final.id,
                carousel=final.from_carousel,
                batch=final.from_batch,
                restored=final.cleared,
            )
            return self._outcome(final)

    # -- reconciliation ----------------------------------------------------

    def reconcile(self) -> list[str]:
        """Cross-check switch records against batch state and command log.

        Returns a list of problems; an empty list means the three ledgers
        agree. The batch state, the transfer command log and the switch
        records must tell the same story, and at most one batch may be live.
        """

        state = self._store.state()
        problems: list[str] = []
        pending = self.pending()
        if len(pending) > 1:
            problems.append(
                f"{len(pending)} unresolved switches: "
                + ", ".join(record.id for record in pending)
            )

        for record in self.records():
            problems.extend(self._reconcile_record(record))

        live_batches = {
            command.batch
            for command in state.transfer_commands
            if command.batch <= state.belt_batch
        }
        # The belt points at one batch. Two distinct live batches can only
        # appear if retire was skipped while the announcement landed - exactly
        # the midpoint state an open switch must still be around to explain.
        live_batches.add(state.belt_batch)
        if len(live_batches) > 1 and not pending:
            problems.append(
                f"commands for batches {sorted(live_batches)} are both live with "
                "no open switch to explain them"
            )

        for record in self.records():
            if record.status != SWITCH_COMMITTED:
                continue
            logged = sum(
                1
                for load in state.load_records
                if load.switch_id == record.id
            )
            if logged != record.cleared:
                problems.append(
                    f"switch {record.id} records {record.cleared} cleared bags "
                    f"but the load log has {logged} entries tagged to it"
                )
        return problems

    def consistent(self) -> bool:
        return not self.reconcile()

    # -- internals ---------------------------------------------------------

    def _drive(self, switch_id: str) -> SwitchOutcome:
        for step in SWITCH_STEPS:
            record = self.get(switch_id)
            if record.has_step(step):
                continue  # replay: the journal proves this step already happened
            try:
                crashed = self._forward_step(record, step)
            except InjectedFault:
                # The step's own transaction rolled back, so the failure note
                # is committed separately: the switch stays running and the
                # last good marker is still the recovery point.
                self._record_failure(switch_id, step)
                record = self.get(switch_id)
                detail = self.describe(record)
                self._emit(
                    "interrupted",
                    switch_id=switch_id,
                    stranded_end=detail.get("stranded_end", ""),
                    stranded_bags=list(detail.get("stranded_bags", ())),
                    steps_done=list(detail.get("steps_done", ())),
                )
                raise
            if crashed:
                record = self.get(switch_id)
                detail = self.describe(record)
                detail.pop("switch_id", None)
                self._emit(
                    "interrupted",
                    switch_id=switch_id,
                    stranded_end=detail.get("stranded_end", ""),
                    stranded_bags=list(detail.get("stranded_bags", ())),
                    steps_done=list(detail.get("steps_done", ())),
                )
                raise SwitchInterrupted(
                    f"batch switch {switch_id} stopped after {step}",
                    switch_id=switch_id,
                    **detail,
                )
        finished = self.get(switch_id)
        self._emit(
            "committed",
            switch_id=switch_id,
            carousel=finished.carousel,
            batch=finished.batch,
            cleared=finished.cleared,
        )
        return self._outcome(finished)

    def _record_failure(self, switch_id: str, step: str) -> None:
        """Append a failure note without changing the step markers."""

        try:
            record = self.get(switch_id)
            updated = record.evolved(
                failure=f"{step}: {self.fail_in or 'fault'} at {self._clock.now_iso()}"
            )
            with self._store.session() as session:
                session.state.switches[switch_id] = updated
                session.record(
                    "switch.fail",
                    {"switch_id": switch_id, "step": step, "error": "injected fault"},
                    self._clock.now_iso(),
                )
        except Exception:  # pragma: no cover - failure noting must never mask the cause
            pass

    def _forward_step(self, record: SwitchRecord, step: str) -> bool:
        """Commit one forward step. True means a post-commit crash was injected."""

        at = self._clock.now_iso()
        with self._store.session() as session:
            scoped = session.state.switches[record.id]
            marker: dict[str, Any] = {"switch_id": scoped.id, "step": step}
            if step == "clear":
                count, prior = self._belt.apply_clear(
                    session,
                    scoped.from_carousel,
                    scoped.from_batch,
                    switch_id=scoped.id,
                )
                scoped = scoped.evolved(cleared=count, prior_bag_states=prior)
                # The prior states live nowhere else in the log, so the marker
                # itself must carry them for rollback after restart.
                marker["cleared"] = count
                marker["prior_bag_states"] = dict(prior)
            elif step == "announce":
                self._belt.apply_set(session, scoped.carousel, scoped.batch)
            elif step == "retire":
                doomed = self._belt.apply_discard_before(session, scoped.batch)
                scoped = scoped.evolved(
                    retired_commands=[command.as_dict() for command in doomed]
                )
                marker["retired_commands"] = [command.as_dict() for command in doomed]

            if self.fail_in == step:
                raise InjectedFault(f"injected fault during {step}")

            steps_done = list(scoped.steps_done)
            steps_done.append(step)
            if step == "finish":
                scoped = scoped.evolved(
                    steps_done=steps_done,
                    status=SWITCH_COMMITTED,
                    finished_at=at,
                )
                session.record(
                    "switch.status",
                    {
                        "switch_id": scoped.id,
                        "status": SWITCH_COMMITTED,
                        "finished_at": at,
                    },
                    at,
                )
            else:
                scoped = scoped.evolved(steps_done=steps_done)
            session.state.switches[scoped.id] = scoped
            session.record("switch.step", marker, at)
        return self.crash_after == step

    def _compensate(self, record: SwitchRecord, step: str) -> None:
        """Commit the compensation for one forward step, marker in same session."""

        at = self._clock.now_iso()
        with self._store.session() as session:
            scoped = session.state.switches[record.id]
            if not scoped.rollback_done:
                scoped = scoped.evolved(status=SWITCH_ROLLING_BACK)
                session.state.switches[scoped.id] = scoped
                session.record(
                    "switch.status",
                    {"switch_id": scoped.id, "status": SWITCH_ROLLING_BACK},
                    at,
                )

            if step == "retire":
                restored = [
                    TransferCommand.from_dict(item)
                    for item in scoped.retired_commands
                ]
                self._belt.apply_restore_commands(session, restored)
            elif step == "announce":
                self._restore_belt_target(session, scoped)
            elif step == "clear":
                self._restore_loads(session, scoped)

            last = step == ROLLBACK_STEPS[-1]
            rollback_done = list(scoped.rollback_done)
            rollback_done.append(f"rb_{step}")
            status = SWITCH_ROLLED_BACK if last else SWITCH_ROLLING_BACK
            scoped = session.state.switches[record.id].evolved(
                rollback_done=rollback_done,
                status=status,
                finished_at=at if last else session.state.switches[record.id].finished_at,
            )
            session.state.switches[record.id] = scoped
            payload: dict[str, Any] = {"switch_id": record.id, "step": step}
            if last:
                payload["status"] = status
                payload["finished_at"] = at
            session.record("switch.rbstep", payload, at)

    def _restore_belt_target(self, session: Session, record: SwitchRecord) -> None:
        """rb_announce: point the belt back at the pre-switch carousel/batch."""

        session.state.belt_carousel = record.from_carousel
        session.state.belt_batch = record.from_batch
        session.record(
            "belt.restore",
            {
                "carousel": record.from_carousel,
                "batch": record.from_batch,
                "on_carousel": list(session.state.belt_on_carousel),
            },
            self._clock.now_iso(),
        )

    def _restore_loads(self, session: Session, record: SwitchRecord) -> None:
        """rb_clear: withdraw the clear, restoring the old batch exactly.

        The bags come back in their original order with their original states;
        the load records this switch wrote are withdrawn and the loaded
        counter is reversed, so the ledgers balance again.
        """

        at = self._clock.now_iso()
        withdrawn = sum(
            1
            for load in session.state.load_records
            if load.switch_id == record.id
        )
        session.state.load_records = [
            load
            for load in session.state.load_records
            if load.switch_id != record.id
        ]
        session.record("load.remove", {"switch_id": record.id}, at)
        for bag_id in record.bag_ids:
            prior = record.prior_bag_states.get(bag_id, BagState.LOADED.value)
            bag = session.state.bags.get(bag_id)
            if bag is not None and bag.state.value != prior:
                updated = bag.evolved(state=BagState(prior))
                session.state.bags[bag_id] = updated
                session.record(
                    "bag.setstate", {"bag_id": bag_id, "state": prior}, at
                )
            if bag_id not in session.state.belt_on_carousel:
                session.state.belt_on_carousel.append(bag_id)
        session.state.belt_carousel = record.from_carousel
        session.state.belt_batch = record.from_batch
        session.record(
            "belt.restore",
            {
                "carousel": record.from_carousel,
                "batch": record.from_batch,
                "on_carousel": list(record.bag_ids),
            },
            at,
        )
        if withdrawn:
            session.record(
                "counter.add", {"name": "belt.loaded", "delta": -withdrawn}, at
            )

    def _reconcile_record(self, record: SwitchRecord) -> list[str]:
        problems: list[str] = []
        known = set(SWITCH_STEPS)
        for step in record.steps_done:
            if step not in known:
                problems.append(f"switch {record.id} logs unknown step {step!r}")
        ordered = [step for step in SWITCH_STEPS if step in record.steps_done]
        if ordered != record.steps_done:
            problems.append(f"switch {record.id} committed steps out of order")

        state = self._store.state()
        if record.status == SWITCH_COMMITTED:
            if record.steps_done != list(SWITCH_STEPS):
                problems.append(
                    f"switch {record.id} is committed without every step marked"
                )
            if (state.belt_carousel, state.belt_batch) != (
                record.carousel,
                record.batch,
            ):
                problems.append(
                    f"switch {record.id} committed but the belt points at another batch"
                )
            stale = [c for c in state.transfer_commands if c.batch < record.batch]
            if stale:
                problems.append(
                    f"switch {record.id} committed with {len(stale)} stale commands live"
                )
            logged = sum(
                1 for load in state.load_records if load.switch_id == record.id
            )
            if logged != record.cleared:
                problems.append(
                    f"switch {record.id} load log ({logged}) disagrees with "
                    f"cleared count ({record.cleared})"
                )
        elif record.status == SWITCH_ROLLED_BACK:
            expected = [
                f"rb_{step}" for step in ROLLBACK_STEPS if step in record.steps_done
            ]
            if record.rollback_done != expected:
                problems.append(
                    f"switch {record.id} is rolled back but compensations are incomplete"
                )
            if (state.belt_carousel, state.belt_batch) != (
                record.from_carousel,
                record.from_batch,
            ):
                problems.append(
                    f"switch {record.id} rolled back but the belt stayed on the new batch"
                )
            if set(record.bag_ids) - set(state.belt_on_carousel):
                problems.append(
                    f"switch {record.id} rolled back but cleared bags are not back on the carousel"
                )
            tagged = [load for load in state.load_records if load.switch_id == record.id]
            if tagged:
                problems.append(
                    f"switch {record.id} rolled back but {len(tagged)} of its load records remain"
                )
        return problems

    def _loaded_bag_impact(self, record: SwitchRecord) -> tuple[list[str], list[str]]:
        """Which loaded bags each decision acts on.

        Every bag on the carousel is marked LOADED by ``clear``. Resume keeps
        that outcome, so all cleared bags finish the day loaded. Rollback only
        *unloads* bags the clear itself loaded: bags already loaded before the
        switch began are left exactly as they were.
        """

        loaded_by_clear = [
            bag_id
            for bag_id in record.bag_ids
            if record.prior_bag_states.get(bag_id, BagState.LOADED.value)
            != BagState.LOADED.value
        ]
        return loaded_by_clear, loaded_by_clear

    def _stranded(self, record: SwitchRecord) -> tuple[str, list[str]]:
        """Which cut-over side is stuck, naming the bags caught in between.        * before ``announce``: the old batch is cleared (bags loaded) but the
          new batch never came up - the *new* side is the one not alive, and
          every bag riding the carousel is caught in the middle;
        * after ``announce``, before ``retire``: the belt already serves the
          new batch while the *old* batch's transfer commands still stand -
          the old side is the one stranded;
        * after ``retire``: only the finish marker remains, no physical side
          is stuck.
        """

        if record.status in (SWITCH_COMMITTED, SWITCH_ROLLED_BACK):
            return "", []
        if not record.has_step("announce"):
            return "new", list(record.bag_ids)
        if not record.has_step("retire"):
            return "old", []
        return "", []

    def _outcome(self, record: SwitchRecord) -> SwitchOutcome:
        stranded_end, stranded_bags = self._stranded(record)
        restored = record.cleared if record.status == SWITCH_ROLLED_BACK else 0
        return SwitchOutcome(
            switch_id=record.id,
            status=record.status,
            cleared=record.cleared,
            restored=restored,
            carousel=record.carousel,
            batch=record.batch,
            from_carousel=record.from_carousel,
            from_batch=record.from_batch,
            stranded_end=stranded_end,
            stranded_bag_ids=stranded_bags,
            steps_done=list(record.steps_done),
            rollback_done=list(record.rollback_done),
            interrupted=record.status == SWITCH_RUNNING,
        )

    def _require(self, switch_id: str) -> SwitchRecord:
        if switch_id:
            return self.get(switch_id)
        record = self.active()
        if record is None:
            raise ConflictError("no unfinished batch switch")
        return record

    def _next_id(self) -> str:
        existing = {record.id for record in self.records()}
        while True:
            candidate = self._ids.next()
            if candidate not in existing:
                return candidate
