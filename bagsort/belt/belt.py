"""Carousel and batch transfer, with crash-safe switches.

A switch moves the carousel from one batch to the next in three ordered,
indivisible steps:

    0. SWEEP     -- load and clear the bags on the old batch;
    1. ANNOUNCE  -- point the belt at the new carousel / batch;
    2. RETIRE    -- retire the previous batch's transfer commands.

Each step is one journal commit that carries both the physical effect and the
switch plan's progress. A crash therefore lands *between* steps, never inside
one, and restart-by-replay rebuilds the plan exactly as far as it got. A switch
that did not reach the final step is left ``in_progress``; an operator then
chooses to **continue** (replay what is done, run only the steps still missing)
or to **roll back** (a compensating commit that restores the old batch). The
plan, the command journal and the audit chain are the three ledgers that have to
agree, and :meth:`Belt.reconcile` checks that they do.

The unguarded ordering -- announce before clear, or a stale command replayed to
a carousel it already left -- is exactly how bags ride across batches, so the
plan is also what keeps the ordering honest under failure.
"""

from __future__ import annotations

from bagsort.clock import Clock
from bagsort.errors import ConflictError, NotFoundError, OrderingError, SwitchInterrupted
from bagsort.ids import IdFactory
from bagsort.model.entities import Bag, ClearedBag, LoadRecord, SwitchPlan, TransferCommand
from bagsort.model.enums import BagState
from bagsort.model.validate import validate_transfer
from bagsort.store.store import Store

# Lifecycle of a switch plan.
IN_PROGRESS = "in_progress"
COMPLETED = "completed"
ROLLED_BACK = "rolled_back"

# Operator decisions once a switch is stranded.
CONTINUE = "continue"
ROLLBACK = "rollback"

# The ordered steps, as the count already committed (0 = nothing yet).
SWEEP_DONE = 1
ANNOUNCE_DONE = 2
ALL_STEPS = 3


class Belt:
    """The outbound carousel and the transfer commands that feed it."""

    def __init__(self, store: Store, clock: Clock, ids: IdFactory) -> None:
        self._store = store
        self._clock = clock
        self._ids = ids
        self._switch_ids = IdFactory("sw", start=self._seeded_switch_count())

    def _seeded_switch_count(self) -> int:
        # Reopening a store must not hand an id a previous switch already owns.
        return len(self._store.state().switches)

    # ------------------------------------------------------------------
    # read side
    # ------------------------------------------------------------------

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

    def switch_status(self) -> dict[str, object]:
        active = self.active_switch()
        return {
            "switch": active.id if active else "",
            "status": active.status if active else "",
            "steps_done": active.steps_done if active else 0,
        }

    def on_carousel(self) -> list[Bag]:
        state = self._store.state()
        return [state.bags[bag_id] for bag_id in state.belt_on_carousel if bag_id in state.bags]

    def loads(self) -> list[LoadRecord]:
        return list(self._store.state().load_records)

    def transfers(self) -> list[TransferCommand]:
        return list(self._store.state().transfer_commands)

    def switch_records(self) -> list[SwitchPlan]:
        return list(self._store.state().switches)

    def active_switch(self) -> SwitchPlan | None:
        """The switch stranded mid-flight, if restart left one behind."""

        for plan in reversed(self._store.state().switches):
            if plan.status == IN_PROGRESS:
                return plan
        return None

    # ------------------------------------------------------------------
    # ordinary, non-switch operations
    # ------------------------------------------------------------------

    def set(self, carousel: str, batch: int) -> dict[str, object]:
        if batch < 0:
            raise OrderingError("batch number must not be negative")
        with self._store.session() as session:
            session.state.belt_carousel = carousel.strip()
            session.state.belt_batch = batch
            session.record(
                "belt.set",
                {"carousel": carousel.strip(), "batch": batch},
                self._clock.now_iso(),
            )
        return self.describe()

    def place(self, bag: Bag) -> int:
        with self._store.session() as session:
            if bag.id not in session.state.belt_on_carousel:
                session.state.belt_on_carousel.append(bag.id)
            session.record("belt.place", {"bag_id": bag.id}, self._clock.now_iso())
        return len(self._store.state().belt_on_carousel)

    def place_batch(self, bags: list[Bag]) -> int:
        for bag in bags:
            self.place(bag)
        return len(self._store.state().belt_on_carousel)

    def rotate(self) -> int:
        """Retire transfer commands older than the running batch."""

        if not self.carousel():
            return 0
        return self.discard_stale(self.batch())

    def append_transfer(self, flight_id: str, carousel: str, batch: int) -> TransferCommand:
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
            session.state.transfer_commands = [
                command
                for command in session.state.transfer_commands
                if command.batch >= floor
            ]
            session.record(
                "transfer.clear_before", {"batch": floor}, self._clock.now_iso()
            )
        return len(doomed)

    # ------------------------------------------------------------------
    # switching: begin, drive one step, finish or compensate
    # ------------------------------------------------------------------

    def switch(self, carousel: str, batch: int) -> int:
        """Clear the current batch, announce the next one, retire old commands.

        Backwards-compatible entry point: begins a crash-safe plan and drives it
        through every step. Returns how many bags the sweep loaded.
        """

        plan = self.begin_switch(carousel, batch)
        cleared = len(plan.cleared)
        self._drive(plan)
        return cleared

    def begin_switch(self, carousel: str, batch: int, fail_after: int | None = None) -> SwitchPlan:
        """Record the intent to switch, before any physical effect commits.

        ``fail_after`` is an injected fault for drills: -1 strands the switch
        immediately after the plan is recorded, otherwise the switch is stranded
        after committing that many steps. Production callers leave it unset.
        """

        if batch < 0:
            raise OrderingError("batch number must not be negative")
        target = carousel.strip()
        if self.active_switch() is not None:
            raise ConflictError("a batch switch is already in progress; resume or roll it back")
        state = self._store.state()
        plan = SwitchPlan(
            id=self._switch_ids.next(),
            from_carousel=state.belt_carousel,
            from_batch=state.belt_batch,
            to_carousel=target,
            to_batch=batch,
            status=IN_PROGRESS,
            on_carousel_snapshot=tuple(state.belt_on_carousel),
            began_at=self._clock.now_iso(),
        )
        with self._store.session() as session:
            session.state.switches.append(plan)
            session.record("switch.begin", {"plan": plan.as_dict()}, plan.began_at)
        if fail_after == -1:
            raise SwitchInterrupted(plan.id, 0)
        plan = self._drive(plan, fail_after=fail_after)
        return plan

    def _drive(self, plan: SwitchPlan, fail_after: int | None = None) -> SwitchPlan:
        """Run every step the plan still owes, each in its own commit."""

        while plan.status == IN_PROGRESS and plan.steps_done < ALL_STEPS:
            next_step = plan.steps_done + 1
            plan = self._advance(plan, next_step)
            if fail_after is not None and next_step >= fail_after:
                raise SwitchInterrupted(plan.id, next_step)
        return plan

    def _advance(self, plan: SwitchPlan, step: int) -> SwitchPlan:
        """Commit the single next step; replay-safe because it keys off steps_done."""

        if step == 1:
            return self._effect_sweep(plan)
        if step == 2:
            return self._effect_announce(plan)
        return self._effect_retire(plan)

    def _effect_sweep(self, plan: SwitchPlan) -> SwitchPlan:
        state = self._store.state()
        pending = list(state.belt_on_carousel)
        at = self._clock.now_iso()
        cleared = list(plan.cleared)
        with self._store.session() as session:
            for bag_id in pending:
                bag = session.state.bags.get(bag_id)
                prior = bag.state.value if bag is not None else BagState.LOADED.value
                if bag is not None and bag.state is not BagState.LOADED:
                    updated = bag.evolved(state=BagState.LOADED)
                    session.state.bags[bag_id] = updated
                    session.record("bag.put", {"bag": updated.as_dict()}, at)
                cleared.append(
                    ClearedBag(
                        bag_id=bag_id, prior_state=prior, prior_on_carousel=True, at=at
                    )
                )
                record = LoadRecord(
                    bag_id=bag_id,
                    carousel=plan.from_carousel,
                    batch=plan.from_batch,
                    at=at,
                )
                session.state.load_records.append(record)
                session.record("load.append", {"record": record.as_dict()}, at)
            session.state.belt_on_carousel = []
            session.record(
                "belt.clear",
                {"carousel": plan.from_carousel, "batch": plan.from_batch},
                at,
            )
            session.record(
                "counter.add", {"name": "belt.loaded", "delta": len(pending)}, at
            )
            advanced = plan.evolved(
                steps_done=SWEEP_DONE, cleared=tuple(cleared)
            )
            self._save_plan(session, advanced, at)
        return advanced

    def _effect_announce(self, plan: SwitchPlan) -> SwitchPlan:
        at = self._clock.now_iso()
        with self._store.session() as session:
            session.state.belt_carousel = plan.to_carousel
            session.state.belt_batch = plan.to_batch
            session.record(
                "belt.set",
                {"carousel": plan.to_carousel, "batch": plan.to_batch},
                at,
            )
            advanced = plan.evolved(steps_done=ANNOUNCE_DONE)
            self._save_plan(session, advanced, at)
        return advanced

    def _effect_retire(self, plan: SwitchPlan) -> SwitchPlan:
        at = self._clock.now_iso()
        retired = [
            command
            for command in self._store.state().transfer_commands
            if command.batch < plan.to_batch
        ]
        with self._store.session() as session:
            if retired:
                session.state.transfer_commands = [
                    command
                    for command in session.state.transfer_commands
                    if command.batch >= plan.to_batch
                ]
                session.record(
                    "transfer.clear_before", {"batch": plan.to_batch}, at
                )
            advanced = plan.evolved(
                steps_done=ALL_STEPS,
                status=COMPLETED,
                decision=CONTINUE,
                retired_transfers=tuple(retired),
                finished_at=at,
            )
            self._save_plan(session, advanced, at)
        return advanced

    def resume_switch(self, decision: str) -> SwitchPlan:
        """Finish a stranded switch by ``continue``-ing it or rolling it back."""

        plan = self.active_switch()
        if plan is None:
            raise ConflictError("no batch switch is in progress")
        choice = decision.strip().lower()
        if choice == CONTINUE:
            return self._drive(plan)
        if choice == ROLLBACK:
            return self._rollback(plan)
        raise OrderingError(f"unknown switch decision {decision!r}; use continue or rollback")

    def _rollback(self, plan: SwitchPlan) -> SwitchPlan:
        """One compensating commit restoring the batch as it was before the switch."""

        state = self._store.state()
        if plan.steps_done >= ANNOUNCE_DONE:
            # The new batch is live. Anything placed on it after the announce is a
            # real bag the rollback would displace, so refuse rather than lose it.
            newcomers = [
                bag_id
                for bag_id in state.belt_on_carousel
                if bag_id not in plan.on_carousel_snapshot
            ]
            if newcomers:
                raise ConflictError(
                    "cannot roll back while the new batch holds bags "
                    f"{sorted(newcomers)}; divert them first"
                )
        at = self._clock.now_iso()
        # Only bags the sweep itself moved into LOADED are undone: a bag that
        # was already loaded keeps that state and keeps no compensating record.
        swept_active = [
            item for item in plan.cleared if item.prior_state != BagState.LOADED.value
        ]
        restored_ids = [item.bag_id for item in plan.cleared if item.prior_on_carousel]
        with self._store.session() as session:
            if plan.steps_done >= SWEEP_DONE:
                removed_at = {item.at for item in swept_active}
                removed = {item.bag_id for item in swept_active}
                for item in swept_active:
                    bag = session.state.bags.get(item.bag_id)
                    if bag is not None:
                        restored_bag = bag.evolved(state=BagState(item.prior_state))
                        session.state.bags[item.bag_id] = restored_bag
                        session.record(
                            "bag.put", {"bag": restored_bag.as_dict()}, at
                        )
                session.state.load_records = [
                    record
                    for record in session.state.load_records
                    if not (
                        record.bag_id in removed
                        and record.at in removed_at
                        and record.carousel == plan.from_carousel
                        and record.batch == plan.from_batch
                    )
                ]
                session.record(
                    "load.remove_many",
                    {
                        "bag_ids": sorted(removed),
                        "carousel": plan.from_carousel,
                        "batch": plan.from_batch,
                        "at": sorted(removed_at),
                    },
                    at,
                )
                # Hand back the transfer commands the (possible) retire step dropped.
                if plan.retired_transfers:
                    session.state.transfer_commands = list(session.state.transfer_commands)
                    existing = {command.id for command in session.state.transfer_commands}
                    session.state.transfer_commands.extend(
                        command
                        for command in plan.retired_transfers
                        if command.id not in existing
                    )
                    session.record(
                        "transfer.restore",
                        {"commands": [c.as_dict() for c in plan.retired_transfers]},
                        at,
                    )
            # Repoint the belt at the old batch and put the swept bags back on it.
            session.state.belt_on_carousel = restored_ids
            session.state.belt_carousel = plan.from_carousel
            session.state.belt_batch = plan.from_batch
            session.record(
                "belt.restore",
                {
                    "carousel": plan.from_carousel,
                    "batch": plan.from_batch,
                    "on_carousel": restored_ids,
                },
                at,
            )
            loaded = int(session.state.counters.get("belt.loaded", 0))
            session.state.counters["belt.loaded"] = max(0, loaded - len(restored_ids))
            session.record(
                "counter.set",
                {"name": "belt.loaded", "value": max(0, loaded - len(restored_ids))},
                at,
            )
            finished = plan.evolved(
                status=ROLLED_BACK,
                decision=ROLLBACK,
                restored=tuple(restored_ids),
                finished_at=at,
            )
            self._save_plan(session, finished, at)
        return finished

    def _save_plan(self, session, plan: SwitchPlan, at: str) -> None:
        session.state.switches = [
            plan if existing.id == plan.id else existing
            for existing in session.state.switches
        ]
        session.record("switch.update", {"plan": plan.as_dict()}, at)

    # ------------------------------------------------------------------
    # decision support and reconciliation
    # ------------------------------------------------------------------

    def switch_decision(self) -> dict[str, object]:
        """Lay out what each choice costs and which end is currently stuck.

        This is read-only: it names the bags each option would leave wrongly
        loaded so an operator picks with the trade-off in front of them.
        """

        plan = self.active_switch()
        if plan is None:
            return {"in_progress": False}

        state = self._store.state()
        # Bags the sweep force-loaded off the old batch: continuing finalises
        # those loads, so a bag that still belonged to the new trip is "lost" to
        # the old batch. Rolling back instead reinstates them.
        forced_loaded = [
            item.bag_id
            for item in plan.cleared
            if item.prior_state != BagState.LOADED.value
        ]
        # Bags that reached the live carousel after the announce: rollback would
        # have to displace them, continuing never touches them.
        newcomers = [
            bag_id
            for bag_id in state.belt_on_carousel
            if bag_id not in plan.on_carousel_snapshot
        ] if plan.steps_done >= ANNOUNCE_DONE else []

        if plan.steps_done == 0:
            stuck_end = "old"
            stuck_reason = "switch recorded but nothing has run; the old batch is still the live one"
        elif plan.steps_done == SWEEP_DONE:
            stuck_end = "new"
            stuck_reason = "old batch swept and cleared, new batch not announced; bags are stranded on the belt"
        else:
            stuck_end = "old"
            stuck_reason = "new batch announced but the old batch's transfer commands are not retired"

        return {
            "in_progress": True,
            "switch": plan.id,
            "steps_done": plan.steps_done,
            "step_total": ALL_STEPS,
            "stuck_end": stuck_end,
            "stuck_reason": stuck_reason,
            "from": {"carousel": plan.from_carousel, "batch": plan.from_batch},
            "to": {"carousel": plan.to_carousel, "batch": plan.to_batch},
            "continue": {
                "remaining_steps": ALL_STEPS - plan.steps_done,
                "finalises_loads": forced_loaded,
                "displaces_new_batch": [],
            },
            "rollback": {
                "restores_to_carousel": [
                    item.bag_id for item in plan.cleared if item.prior_on_carousel
                ],
                "removes_load_records": forced_loaded,
                "displaces_new_batch": newcomers,
                "blocked": bool(newcomers),
            },
        }

    def reconcile(self) -> dict[str, object]:
        """Cross-check the switch records, the command journal and the belt state.

        Returns one report; ``ok`` is true only when the three ledgers agree and
        the plant invariants hold (one active batch, swept bags accounted for).
        """

        state = self._store.state()
        problems: list[str] = []

        active = [plan for plan in state.switches if plan.status == IN_PROGRESS]
        if len(active) > 1:
            problems.append(f"{len(active)} switches in progress at once")
        current = active[0] if active else None

        # 1. Plan ledger vs. belt state: how far the plan says it got must match
        #    which carousel/batch the journal has actually materialised.
        if current is not None:
            if current.steps_done == 0:
                if (
                    state.belt_carousel != current.from_carousel
                    or state.belt_batch != current.from_batch
                ):
                    problems.append("plan at step 0 but the belt already left the old batch")
            elif current.steps_done == SWEEP_DONE:
                if state.belt_on_carousel:
                    problems.append("swept switch still has bags on the carousel")
                if (
                    state.belt_carousel != current.from_carousel
                    or state.belt_batch != current.from_batch
                ):
                    problems.append("swept switch announces a batch the plan has not run")
            elif current.steps_done == ANNOUNCE_DONE:
                if (
                    state.belt_carousel != current.to_carousel
                    or state.belt_batch != current.to_batch
                ):
                    problems.append("announced switch is not pointing at the new batch")
            else:
                problems.append("in-progress plan reports every step done")

        # 2. Two batches must never be live at once: with the new batch
        #    announced, no transfer command older than it may survive.
        if current is not None and current.steps_done >= ANNOUNCE_DONE:
            stale = [c.batch for c in state.transfer_commands if c.batch < current.to_batch]
            if stale:
                problems.append(f"old transfer commands still live for the new batch: {stale}")

        # 3. Every bag a switch force-loaded must have a matching load record while
        #    in progress or completed, and none once rolled back. A bag already
        #    loaded before the sweep is not force-loaded and is not reconciled here.
        for plan in state.switches:
            for item in plan.cleared:
                if item.prior_state == BagState.LOADED.value:
                    continue
                matching = [
                    record
                    for record in state.load_records
                    if record.bag_id == item.bag_id
                    and record.carousel == plan.from_carousel
                    and record.batch == plan.from_batch
                    and record.at == item.at
                ]
                if plan.status in (IN_PROGRESS, COMPLETED):
                    if not matching:
                        problems.append(
                            f"switch {plan.id} swept {item.bag_id} but its load record is missing"
                        )
                elif plan.status == ROLLED_BACK:
                    if matching:
                        problems.append(
                            f"switch {plan.id} rolled back but {item.bag_id} still shows a load"
                        )
                    if item.bag_id in plan.restored and item.bag_id not in state.belt_on_carousel:
                        problems.append(
                            f"switch {plan.id} says {item.bag_id} restored but it is not on the belt"
                        )

        return {
            "ok": not problems,
            "problems": problems,
            "active_switch": current.id if current else "",
            "active_status": current.status if current else "",
            "carousel": state.belt_carousel,
            "batch": state.belt_batch,
            "switches": len(state.switches),
        }

    def get_switch(self, switch_id: str) -> SwitchPlan:
        for plan in reversed(self._store.state().switches):
            if plan.id == switch_id:
                return plan
        raise NotFoundError(f"no switch {switch_id!r}")
