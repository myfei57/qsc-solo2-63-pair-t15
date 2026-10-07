"""Crash-safe carousel batch switching.

A switch runs in three committed steps (sweep, announce, retire). A crash lands
between steps, leaving the switch ``in_progress``; an operator then continues
(replay what is done, run only the rest) or rolls back (restore the old batch).
These tests strand the switch at every boundary, including after a real restart,
and assert the three ledgers agree either way.
"""

from __future__ import annotations

import pytest

from bagsort.belt.belt import (
    ANNOUNCE_DONE,
    COMPLETED,
    ROLLED_BACK,
    SWEEP_DONE,
)
from bagsort.errors import SwitchInterrupted
from bagsort.model.enums import BagState
from bagsort.store.store import Store
from bagsort.app import Plant
from tests.support import check_in


def _crash_after(plant, carousel: str, batch: int, step: int):
    """Begin a switch, let it strand after ``step`` committed steps."""

    with pytest.raises(SwitchInterrupted):
        plant.switch_belt(carousel, batch, crash_after=step)
    return plant.belt.active_switch()


def _reopen(plant) -> Plant:
    """Persist and rebuild the plant, exactly like a process restart."""

    path = plant.store.path
    plant.store.persist()
    return Plant.build(Store.open(path), terminal=plant.namespace.terminal, clock=plant.clock)


# ---------------------------------------------------------------------------
# the happy path still drives all three steps and closes the plan
# ---------------------------------------------------------------------------


def test_a_clean_switch_completes_and_keeps_the_legacy_contract(plant, flight):
    plant.belt.set("T1-CRCR1", 1)
    bags = check_in(plant, flight.id, 2)
    plant.belt.place_batch(bags)

    cleared = plant.switch_belt("T1-CRCR2", 2)

    assert cleared == 2
    assert plant.belt.carousel() == "T1-CRCR2"
    assert plant.belt.batch() == 2
    assert plant.belt.on_carousel() == []
    assert plant.belt.active_switch() is None
    plan = plant.belt.switch_records()[-1]
    assert plan.status == COMPLETED
    assert plan.steps_done == 3


def test_a_clean_switch_retires_old_transfer_commands(plant, flight):
    plant.belt.set("T1-CRCR1", 1)
    plant.append_transfer(flight.id, "T1-CRCR1", 1)
    plant.switch_belt("T1-CRCR2", 2)
    assert plant.belt.transfers() == []
    plan = plant.belt.switch_records()[-1]
    assert len(plan.retired_transfers) == 1


# ---------------------------------------------------------------------------
# stranding at every boundary
# ---------------------------------------------------------------------------


def test_strand_before_any_step_leaves_the_old_batch_intact(plant, flight):
    check_in(plant, flight.id, 1)
    plant.belt.set("T1-CRCR1", 1)
    bags = check_in(plant, flight.id, 1)
    plant.belt.place_batch(bags)

    plan = _crash_after(plant, "T1-CRCR2", 2, -1)

    assert plan.steps_done == 0
    assert plant.belt.carousel() == "T1-CRCR1"
    assert len(plant.belt.on_carousel()) == 1


def test_strand_after_sweep_clears_old_but_does_not_announce(plant, flight):
    check_in(plant, flight.id, 1)
    plant.belt.set("T1-CRCR1", 1)
    bags = check_in(plant, flight.id, 2)
    plant.belt.place_batch(bags)

    plan = _crash_after(plant, "T1-CRCR2", 2, SWEEP_DONE)

    assert plan.steps_done == SWEEP_DONE
    assert plant.belt.carousel() == "T1-CRCR1"  # new batch not announced yet
    assert plant.belt.batch() == 1
    assert plant.belt.on_carousel() == []  # old swept clear: bags stranded
    swept = {plant.bag(bag.id).state for bag in bags}
    assert swept == {BagState.LOADED}
    assert len(plant.belt.loads()) == 2


def test_strand_after_announce_points_at_new_but_old_commands_live(plant, flight):
    plant.belt.set("T1-CRCR1", 1)
    plant.append_transfer(flight.id, "T1-CRCR1", 1)

    plan = _crash_after(plant, "T1-CRCR2", 2, ANNOUNCE_DONE)

    assert plan.steps_done == ANNOUNCE_DONE
    assert (plant.belt.carousel(), plant.belt.batch()) == ("T1-CRCR2", 2)
    # the retire step never ran: old commands still present
    assert [c.batch for c in plant.belt.transfers()] == [1]


# ---------------------------------------------------------------------------
# continue: replay done steps, run only the missing ones
# ---------------------------------------------------------------------------


def test_continue_after_sweep_finishes_only_the_remaining_steps(plant, flight):
    check_in(plant, flight.id, 1)
    plant.belt.set("T1-CRCR1", 1)
    bags = check_in(plant, flight.id, 2)
    plant.belt.place_batch(bags)
    _crash_after(plant, "T1-CRCR2", 2, SWEEP_DONE)

    loads_before = len(plant.belt.loads())
    plan = plant.resume_switch("continue")

    assert plan.status == COMPLETED
    assert (plant.belt.carousel(), plant.belt.batch()) == ("T1-CRCR2", 2)
    assert plant.belt.active_switch() is None
    # the sweep was already durable; continuing must not sweep or load twice
    assert len(plant.belt.loads()) == loads_before
    assert len(plan.cleared) == 2


def test_continue_after_announce_retires_without_touching_the_sweep(plant, flight):
    plant.belt.set("T1-CRCR1", 1)
    plant.append_transfer(flight.id, "T1-CRCR1", 1)
    _crash_after(plant, "T1-CRCR2", 2, ANNOUNCE_DONE)

    plan = plant.resume_switch("continue")

    assert plan.status == COMPLETED
    assert plant.belt.transfers() == []
    assert plant.belt.loads() == []  # nothing was ever on the carousel


def test_continue_is_idempotent_under_repeated_drive(plant, flight):
    plant.belt.set("T1-CRCR1", 1)
    _crash_after(plant, "T1-CRCR2", 2, SWEEP_DONE)
    first = plant.resume_switch("continue")
    # a second resume has nothing to do
    with pytest.raises(Exception):
        plant.resume_switch("continue")
    assert first.status == COMPLETED


# ---------------------------------------------------------------------------
# continue after a real restart: replay the journal, fill only the gaps
# ---------------------------------------------------------------------------


def test_restart_rebuilds_the_stranded_switch_and_continue_finishes(plant, flight):
    check_in(plant, flight.id, 1)
    plant.belt.set("T1-CRCR1", 1)
    bags = check_in(plant, flight.id, 3)
    plant.belt.place_batch(bags)
    _crash_after(plant, "T1-CRCR2", 2, SWEEP_DONE)

    reopened = _reopen(plant)

    active = reopened.belt.active_switch()
    assert active is not None
    assert active.steps_done == SWEEP_DONE
    assert len(active.cleared) == 3
    assert reopened.belt.carousel() == "T1-CRCR1"
    assert len(reopened.belt.on_carousel()) == 0

    loads_before = len(reopened.belt.loads())
    plan = reopened.resume_switch("continue")

    assert plan.status == COMPLETED
    assert (reopened.belt.carousel(), reopened.belt.batch()) == ("T1-CRCR2", 2)
    assert len(reopened.belt.loads()) == loads_before  # no double sweep on replay
    assert reopened.reconcile_belt()["ok"] is True


def test_restart_after_announce_then_continue_is_consistent(plant, flight):
    plant.belt.set("T1-CRCR1", 1)
    plant.append_transfer(flight.id, "T1-CRCR1", 1)
    _crash_after(plant, "T1-CRCR2", 2, ANNOUNCE_DONE)

    reopened = _reopen(plant)
    assert reopened.belt.active_switch().steps_done == ANNOUNCE_DONE
    plan = reopened.resume_switch("continue")

    assert plan.status == COMPLETED
    assert reopened.belt.transfers() == []
    assert reopened.reconcile_belt()["ok"] is True


# ---------------------------------------------------------------------------
# rollback: restore the already-cleared old batch
# ---------------------------------------------------------------------------


def test_rollback_after_sweep_restores_bags_load_records_and_batch(plant, flight):
    check_in(plant, flight.id, 1)
    plant.belt.set("T1-CRCR1", 1)
    bags = check_in(plant, flight.id, 2)
    prior_states = {bag.id: bag.state for bag in bags}
    plant.belt.place_batch(bags)
    _crash_after(plant, "T1-CRCR2", 2, SWEEP_DONE)
    assert len(plant.belt.loads()) == 2

    plan = plant.resume_switch("rollback")

    assert plan.status == ROLLED_BACK
    assert (plant.belt.carousel(), plant.belt.batch()) == ("T1-CRCR1", 1)
    # bags are back on the old carousel in their prior, non-loaded state
    restored = {bag.id: plant.bag(bag.id) for bag in plant.belt.on_carousel()}
    assert set(restored) == {bag.id for bag in bags}
    for bag_id, prior in prior_states.items():
        assert restored[bag_id].state == prior
    # the compensating load records written by the sweep are gone, and so is the count
    assert plant.belt.loads() == []
    assert plant.store.state().counters.get("belt.loaded", 0) == 0
    assert plant.belt.active_switch() is None
    assert plant.reconcile_belt()["ok"] is True


def test_rollback_after_announce_restores_old_batch_and_its_commands(plant, flight):
    plant.belt.set("T1-CRCR1", 1)
    plant.append_transfer(flight.id, "T1-CRCR1", 1)
    _crash_after(plant, "T1-CRCR2", 2, ANNOUNCE_DONE)

    plan = plant.resume_switch("rollback")

    assert plan.status == ROLLED_BACK
    assert (plant.belt.carousel(), plant.belt.batch()) == ("T1-CRCR1", 1)
    # old commands were never retired (retire step didn't run) and stay intact
    assert [c.batch for c in plant.belt.transfers()] == [1]


def test_rollback_before_any_step_is_a_clean_noop_back_to_old(plant, flight):
    plant.belt.set("T1-CRCR1", 1)
    _crash_after(plant, "T1-CRCR2", 2, -1)

    plan = plant.resume_switch("rollback")

    assert plan.status == ROLLED_BACK
    assert (plant.belt.carousel(), plant.belt.batch()) == ("T1-CRCR1", 1)
    assert plant.belt.loads() == []
    assert plant.reconcile_belt()["ok"] is True


def test_rollback_survives_restart_and_restores(plant, flight):
    check_in(plant, flight.id, 1)
    plant.belt.set("T1-CRCR1", 1)
    bags = check_in(plant, flight.id, 2)
    plant.belt.place_batch(bags)
    _crash_after(plant, "T1-CRCR2", 2, SWEEP_DONE)

    reopened = _reopen(plant)
    plan = reopened.resume_switch("rollback")

    assert plan.status == ROLLED_BACK
    assert len(reopened.belt.on_carousel()) == 2
    assert reopened.belt.loads() == []
    assert reopened.reconcile_belt()["ok"] is True


def test_rollback_refuses_when_the_new_batch_holds_a_bag(plant, flight):
    plant.belt.set("T1-CRCR1", 1)
    _crash_after(plant, "T1-CRCR2", 2, ANNOUNCE_DONE)
    # an operator feeds the live (new) batch before deciding
    late_bag = check_in(plant, flight.id, 1, prefix="LATE")[0]
    plant.belt.place(late_bag)

    from bagsort.errors import ConflictError

    with pytest.raises(ConflictError):
        plant.resume_switch("rollback")
    # nothing changed: the new batch is still live with its bag
    assert plant.belt.active_switch() is not None
    assert late_bag.id in [b.id for b in plant.belt.on_carousel()]


# ---------------------------------------------------------------------------
# decision support: which end is stuck and who gets hurt
# ---------------------------------------------------------------------------


def test_decision_points_at_the_new_end_after_sweep(plant, flight):
    plant.belt.set("T1-CRCR1", 1)
    bags = check_in(plant, flight.id, 1)
    plant.belt.place_batch(bags)
    _crash_after(plant, "T1-CRCR2", 2, SWEEP_DONE)

    decision = plant.switch_decision()

    assert decision["in_progress"] is True
    assert decision["stuck_end"] == "new"
    assert decision["steps_done"] == SWEEP_DONE
    assert decision["continue"]["finalises_loads"] == [bags[0].id]
    assert decision["rollback"]["restores_to_carousel"] == [bags[0].id]
    assert decision["rollback"]["removes_load_records"] == [bags[0].id]


def test_decision_points_at_the_old_end_after_announce(plant, flight):
    plant.belt.set("T1-CRCR1", 1)
    _crash_after(plant, "T1-CRCR2", 2, ANNOUNCE_DONE)

    decision = plant.switch_decision()

    assert decision["stuck_end"] == "old"
    assert decision["continue"]["remaining_steps"] == 1
    assert decision["rollback"]["displaces_new_batch"] == []
    assert decision["rollback"]["blocked"] is False


def test_decision_reports_no_switch_when_idle(plant):
    assert plant.switch_decision()["in_progress"] is False


def test_unknown_decision_is_rejected(plant, flight):
    plant.belt.set("T1-CRCR1", 1)
    _crash_after(plant, "T1-CRCR2", 2, -1)
    with pytest.raises(Exception):
        plant.resume_switch("sideways")


# ---------------------------------------------------------------------------
# mutual exclusion, reconciliation and audit
# ---------------------------------------------------------------------------


def test_cannot_begin_a_second_switch_while_one_is_in_progress(plant, flight):
    plant.belt.set("T1-CRCR1", 1)
    _crash_after(plant, "T1-CRCR2", 2, SWEEP_DONE)
    from bagsort.errors import ConflictError

    with pytest.raises(ConflictError):
        plant.switch_belt("T1-CRCR3", 3)


def test_reconcile_is_clean_when_idle_and_through_each_outcome(plant, flight):
    assert plant.reconcile_belt()["ok"] is True
    plant.belt.set("T1-CRCR1", 1)
    bags = check_in(plant, flight.id, 1)
    plant.belt.place_batch(bags)
    _crash_after(plant, "T1-CRCR2", 2, SWEEP_DONE)
    # stranded mid-flight: swept bags still accounted for, so ledgers agree
    assert plant.reconcile_belt()["ok"] is True
    plant.resume_switch("continue")
    assert plant.reconcile_belt()["ok"] is True


def test_reconcile_flags_two_batches_if_the_journal_is_tampered(plant, flight):
    plant.belt.set("T1-CRCR1", 1)
    plant.append_transfer(flight.id, "T1-CRCR1", 1)
    _crash_after(plant, "T1-CRCR2", 2, ANNOUNCE_DONE)
    # corrupt the ledger: sneak a fresh old-batch transfer command in after announce
    from bagsort.model.entities import TransferCommand

    ghost = TransferCommand(
        id="cmd-ghost",
        flight_id=flight.id,
        carousel="T1-CRCR1",
        batch=0,
        created_at=plant.clock.now_iso(),
    )
    with plant.store.session() as session:
        session.state.transfer_commands.append(ghost)
        session.record("transfer.append", {"command": ghost.as_dict()})
    report = plant.reconcile_belt()
    assert report["ok"] is False
    assert any("transfer commands" in problem for problem in report["problems"])


def test_resuming_publishes_an_audit_trail_for_both_decisions(plant, flight):
    plant.belt.set("T1-CRCR1", 1)
    bags = check_in(plant, flight.id, 1)
    plant.belt.place_batch(bags)
    _crash_after(plant, "T1-CRCR2", 2, SWEEP_DONE)
    plant.resume_switch("continue")

    kinds = plant.audit.by_kind()
    assert kinds.get("belt_switch_failed", 0) == 1
    assert kinds.get("belt_switch_resumed", 0) == 1
    assert plant.audit.verify() is True
    # the stranded point is named in the failure entry
    failed = [e for e in plant.audit.entries() if e.kind == "belt_switch_failed"][0]
    assert failed.detail.get("steps_done") == SWEEP_DONE


def test_rollback_publishes_a_distinct_audit_entry(plant, flight):
    plant.belt.set("T1-CRCR1", 1)
    bags = check_in(plant, flight.id, 1)
    plant.belt.place_batch(bags)
    _crash_after(plant, "T1-CRCR2", 2, SWEEP_DONE)
    plant.resume_switch("rollback")
    kinds = plant.audit.by_kind()
    assert kinds.get("belt_switch_rolled_back", 0) == 1
    assert plant.audit.verify() is True


def test_switch_records_command_log_and_audit_all_reference_the_same_id(plant, flight):
    plant.belt.set("T1-CRCR1", 1)
    _crash_after(plant, "T1-CRCR2", 2, SWEEP_DONE)
    switch_id = plant.belt.active_switch().id
    # the command log carries the plan under this id
    switch_entries = [
        entry
        for entry in plant.store.entries()
        if entry.kind in {"switch.begin", "switch.update"}
        and entry.payload["plan"]["id"] == switch_id
    ]
    assert switch_entries and switch_entries[0].kind == "switch.begin"
    plant.resume_switch("continue")
    audited = [e for e in plant.audit.entries() if switch_id in e.subject]
    assert audited, "audit trail must name the switch it relates to"
