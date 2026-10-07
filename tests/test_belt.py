"""Carousel batch primitives and the crash safe switch saga."""

from __future__ import annotations

import pytest

from bagsort.belt.switching import InjectedFault
from bagsort.errors import (
    ConflictError,
    SwitchInterrupted,
    ValidationError,
)
from bagsort.model.entities import (
    SWITCH_COMMITTED,
    SWITCH_ROLLED_BACK,
    SWITCH_ROLLING_BACK,
    SWITCH_RUNNING,
)
from bagsort.model.enums import BagState
from tests.support import check_in


def test_set_and_describe(plant):
    plant.belt.set("T1-CRCR1", 2)
    assert plant.belt.describe() == {"carousel": "T1-CRCR1", "batch": 2, "on_carousel": 0}


def test_switch_clears_the_current_batch_before_announcing_the_next(plant, flight):
    plant.belt.set("T1-CRCR1", 1)
    bag = check_in(plant, flight.id, 1)[0]
    plant.belt.place(bag)
    outcome = plant.switches.begin("T1-CRCR2", 2)
    assert outcome.status == SWITCH_COMMITTED
    assert outcome.cleared == 1
    assert outcome.steps_done == ["clear", "announce", "retire", "finish"]
    assert plant.belt.describe()["carousel"] == "T1-CRCR2"
    assert plant.belt.describe()["batch"] == 2
    loads = plant.belt.loads()
    assert [(record.carousel, record.batch) for record in loads] == [("T1-CRCR1", 1)]
    assert loads[0].switch_id == outcome.switch_id
    assert plant.switches.consistent() is True


def test_switch_retires_the_previous_batch_transfer_commands(plant, flight):
    plant.belt.set("T1-CRCR1", 1)
    plant.belt.append_transfer(flight.id, "T1-CRCR1", 1)
    plant.switches.begin("T1-CRCR2", 2)
    assert plant.belt.transfers() == []


def test_replay_filters_to_the_running_batch(plant, flight):
    plant.belt.set("T1-CRCR1", 1)
    plant.belt.append_transfer(flight.id, "T1-CRCR1", 1)
    plant.belt.append_transfer(flight.id, "T1-CRCR1", 2)
    assert [command.batch for command in plant.belt.replay_transfers()] == [1]


def test_rotate_retires_older_batches(plant, flight):
    assert plant.belt.rotate() == 0
    plant.belt.set("T1-CRCR1", 3)
    plant.belt.append_transfer(flight.id, "T1-CRCR1", 2)
    plant.belt.append_transfer(flight.id, "T1-CRCR1", 3)
    assert plant.belt.rotate() == 1
    assert [command.batch for command in plant.belt.transfers()] == [3]


def test_place_batch_places_every_bag(plant, flight):
    bags = check_in(plant, flight.id, 3)
    assert plant.belt.place_batch(bags) == 3
    assert len(plant.belt.on_carousel()) == 3


def test_append_transfer_validates_the_payload(plant, flight):
    with pytest.raises(ValidationError):
        plant.belt.append_transfer(flight.id, "   ", 1)


def test_begin_rejects_a_negative_batch(plant):
    with pytest.raises(Exception):
        plant.switches.begin("T1-CRCR1", -1)


# ---------------------------------------------------------------------------
# midpoint failure: decision point, stranded side, and the two recoveries
# ---------------------------------------------------------------------------


def _armed(plant, **faults):
    for name, value in faults.items():
        setattr(plant.switches, name, value)
    return plant.switches


def test_failure_after_clear_flags_the_new_side_and_keeps_the_old_bags_loaded(plant, flight):
    plant.belt.set("T1-CRCR1", 1)
    bag = check_in(plant, flight.id, 1)[0]
    plant.belt.place(bag)
    _armed(plant, crash_after="clear")
    with pytest.raises(SwitchInterrupted) as excinfo:
        plant.switches.begin("T1-CRCR2", 2)
    info = excinfo.value
    assert info.switch_id
    assert info.context["stranded_end"] == "new"
    assert info.context["stranded_bags"] == [bag.id]

    record = plant.switches.get(info.switch_id)
    assert record.status == SWITCH_RUNNING
    assert record.steps_done == ["clear"]
    # old batch already cleared: bags loaded against the old batch...
    assert plant.bag(bag.id).state is BagState.LOADED
    # ...but the new batch never came up, so nothing is live on the carousel.
    assert plant.belt.carousel() == "T1-CRCR1"
    assert plant.belt.batch() == 1
    assert plant.belt.on_carousel() == []
    status = plant.switches.describe(record)
    assert status["resumable"] is True
    assert status["rollback_available"] is True
    # The bags caught in the middle: clear already loaded them, so resume
    # keeps them loaded while rollback would unload them back onto the belt.
    assert status["resume_keeps_loaded"] == [bag.id]
    assert status["rollback_unloads"] == [bag.id]


def test_resume_after_clear_replays_only_the_missing_steps(plant, flight):
    plant.belt.set("T1-CRCR1", 1)
    bags = check_in(plant, flight.id, 2)
    plant.belt.place_batch(bags)
    _armed(plant, crash_after="clear")
    with pytest.raises(SwitchInterrupted) as excinfo:
        plant.switches.begin("T1-CRCR2", 2)
    switch_id = excinfo.value.switch_id

    # Resume runs the same journal path: clear is marked, so it is skipped and
    # only announce/retire/finish are newly committed.
    plant.switches.crash_after = ""
    outcome = plant.switches.resume()
    assert outcome.switch_id == switch_id
    assert outcome.status == SWITCH_COMMITTED
    assert outcome.steps_done == ["clear", "announce", "retire", "finish"]
    assert plant.belt.carousel() == "T1-CRCR2"
    assert [bag.state for bag in (plant.bag(b.id) for b in bags)] == [
        BagState.LOADED,
        BagState.LOADED,
    ]
    # The loaded accounts balance: one load record per cleared bag.
    tagged = [load for load in plant.belt.loads() if load.switch_id == switch_id]
    assert len(tagged) == 2
    assert plant.store.state().counters["belt.loaded"] == 2
    assert plant.switches.consistent() is True


def test_rollback_after_clear_restores_the_old_batch_exactly(plant, flight):
    plant.belt.set("T1-CRCR1", 1)
    bags = check_in(plant, flight.id, 2)
    prior_states = {bag.id: bag.state for bag in bags}
    plant.belt.place_batch(bags)
    _armed(plant, crash_after="clear")
    with pytest.raises(SwitchInterrupted) as excinfo:
        plant.switches.begin("T1-CRCR2", 2)
    switch_id = excinfo.value.switch_id

    outcome = plant.switches.rollback()
    assert outcome.status == SWITCH_ROLLED_BACK
    assert outcome.restored == 2
    assert plant.belt.carousel() == "T1-CRCR1"
    assert plant.belt.batch() == 1
    restored = plant.belt.on_carousel()
    assert [bag.id for bag in restored] == [bag.id for bag in bags]
    assert all(bag.state is prior_states[bag.id] for bag in restored)
    # The clear's accounting is withdrawn, not deleted from history: the
    # compensating load.remove sits in the journal but no load record remains.
    assert [load for load in plant.belt.loads() if load.switch_id == switch_id] == []
    assert plant.store.state().counters["belt.loaded"] == 0
    assert plant.switches.consistent() is True


def test_failure_after_announce_strands_the_old_side_and_rollback_undoes_it(plant, flight):
    plant.belt.set("T1-CRCR1", 1)
    plant.belt.append_transfer(flight.id, "T1-CRCR1", 1)
    bag = check_in(plant, flight.id, 1)[0]
    prior_state = bag.state
    plant.belt.place(bag)
    _armed(plant, crash_after="announce")
    with pytest.raises(SwitchInterrupted) as excinfo:
        plant.switches.begin("T1-CRCR2", 2)
    info = excinfo.value
    # announce landed, retire did not: the new batch serves the belt while the
    # old batch's commands are the stranded side.
    assert info.context["stranded_end"] == "old"
    assert plant.belt.carousel() == "T1-CRCR2"
    assert [command.batch for command in plant.belt.transfers()] == [1]

    outcome = plant.switches.rollback()
    assert outcome.status == SWITCH_ROLLED_BACK
    assert plant.belt.carousel() == "T1-CRCR1"
    # rb_retire brought the retired commands back...
    assert [command.batch for command in plant.belt.transfers()] == [1]
    # ...and rb_clear put the bag back on the old carousel.
    assert [bag.id for bag in plant.belt.on_carousel()] == [bag.id]
    assert plant.bag(bag.id).state is prior_state
    assert plant.switches.consistent() is True


def test_failure_after_retire_can_still_roll_back_through_every_compensation(plant, flight):
    plant.belt.set("T1-CRCR1", 1)
    plant.belt.append_transfer(flight.id, "T1-CRCR1", 1)
    bag = check_in(plant, flight.id, 1)[0]
    plant.belt.place(bag)
    _armed(plant, crash_after="retire")
    with pytest.raises(SwitchInterrupted):
        plant.switches.begin("T1-CRCR2", 2)

    outcome = plant.switches.rollback()
    assert outcome.rollback_done == ["rb_retire", "rb_announce", "rb_clear"]
    assert plant.belt.carousel() == "T1-CRCR1"
    assert [command.batch for command in plant.belt.transfers()] == [1]
    assert [bag.id for bag in plant.belt.on_carousel()] == [bag.id]
    assert plant.switches.consistent() is True


def test_in_session_failure_leaves_nothing_of_that_step_committed(plant, flight):
    plant.belt.set("T1-CRCR1", 1)
    bag = check_in(plant, flight.id, 1)[0]
    plant.belt.place(bag)
    _armed(plant, fail_in="announce")
    with pytest.raises(InjectedFault):
        plant.switches.begin("T1-CRCR2", 2)
    active = plant.switches.active()
    assert active is not None
    assert active.steps_done == ["clear"]
    # The failed step left a failure note in the log but did not move the
    # status off running, so the operator still gets both recovery choices.
    assert "announce" in active.failure
    # The announce transaction rolled back wholesale: belt still points old.
    assert plant.belt.carousel() == "T1-CRCR1"
    plant.switches.fail_in = ""
    outcome = plant.switches.resume(active.id)
    assert outcome.status == SWITCH_COMMITTED


# ---------------------------------------------------------------------------
# crash safety: restart replays the journal and picks exactly the right path
# ---------------------------------------------------------------------------


def test_restart_after_crash_resume_matches_a_live_recovery(plant, flight):
    plant.belt.set("T1-CRCR1", 1)
    bags = check_in(plant, flight.id, 2)
    plant.belt.place_batch(bags)
    _armed(plant, crash_after="clear")
    with pytest.raises(SwitchInterrupted):
        plant.switches.begin("T1-CRCR2", 2)
    plant.store.persist()

    from bagsort.app import Plant
    from bagsort.clock import Clock
    from bagsort.store.store import Store

    restarted = Plant.build(Store.open(plant.store.path), clock=Clock())
    active = restarted.switches.active()
    assert active is not None
    assert active.steps_done == ["clear"]
    # The rollback evidence survived the log replay.
    assert set(active.prior_bag_states) == {bag.id for bag in bags}

    outcome = restarted.switches.resume()
    assert outcome.status == SWITCH_COMMITTED
    assert restarted.belt.carousel() == "T1-CRCR2"
    assert restarted.switches.consistent() is True
    assert restarted.audit.verify() is True


def test_restart_after_crash_rollback_restores_the_old_batch(plant, flight):
    plant.belt.set("T1-CRCR1", 1)
    plant.belt.append_transfer(flight.id, "T1-CRCR1", 1)
    bag = check_in(plant, flight.id, 1)[0]
    prior_state = bag.state
    plant.belt.place(bag)
    _armed(plant, crash_after="announce")
    with pytest.raises(SwitchInterrupted):
        plant.switches.begin("T1-CRCR2", 2)
    plant.store.persist()

    from bagsort.app import Plant
    from bagsort.clock import Clock
    from bagsort.store.store import Store

    restarted = Plant.build(Store.open(plant.store.path), clock=Clock())
    outcome = restarted.switches.rollback()
    assert outcome.status == SWITCH_ROLLED_BACK
    assert restarted.belt.carousel() == "T1-CRCR1"
    assert [bag.id for bag in restarted.belt.on_carousel()] == [bag.id]
    assert restarted.bag(bag.id).state is prior_state
    assert [command.batch for command in restarted.belt.transfers()] == [1]
    assert restarted.switches.consistent() is True


def test_crash_during_rollback_resumes_the_compensation_from_its_marker(plant, flight):
    plant.belt.set("T1-CRCR1", 1)
    plant.belt.append_transfer(flight.id, "T1-CRCR1", 1)
    bag = check_in(plant, flight.id, 1)[0]
    plant.belt.place(bag)
    _armed(plant, crash_after="retire")
    with pytest.raises(SwitchInterrupted):
        plant.switches.begin("T1-CRCR2", 2)
    record = plant.switches.active()

    # Run rb_retire, then die before rb_announce commits: emulate by applying
    # just the first compensation through the internal helper.
    plant.switches._compensate(record, "retire")
    mid = plant.switches.get(record.id)
    assert mid.status == SWITCH_ROLLING_BACK
    assert mid.rollback_done == ["rb_retire"]

    outcome = plant.switches.rollback()
    assert outcome.status == SWITCH_ROLLED_BACK
    assert outcome.rollback_done == ["rb_retire", "rb_announce", "rb_clear"]
    assert plant.switches.consistent() is True


# ---------------------------------------------------------------------------
# guards: never two batches live, no touching the belt mid cut-over
# ---------------------------------------------------------------------------


def test_a_second_switch_cannot_start_while_one_is_open(plant):
    plant.belt.set("T1-CRCR1", 1)
    _armed(plant, crash_after="clear")
    with pytest.raises(SwitchInterrupted):
        plant.switches.begin("T1-CRCR2", 2)
    with pytest.raises(ConflictError):
        plant.switches.begin("T1-CRCR3", 3)


def test_belt_mutations_are_blocked_while_a_switch_is_open(plant, flight):
    plant.belt.set("T1-CRCR1", 1)
    bag = check_in(plant, flight.id, 1)[0]
    plant.belt.place(bag)
    _armed(plant, crash_after="clear")
    with pytest.raises(SwitchInterrupted):
        plant.switches.begin("T1-CRCR2", 2)
    with pytest.raises(ConflictError):
        plant.belt.place(bag)
    with pytest.raises(ConflictError):
        plant.belt.set("T1-CRCR9", 9)
    with pytest.raises(ConflictError):
        plant.belt.append_transfer(flight.id, "T1-CRCR2", 2)
    plant.switches.rollback()
    # The belt is usable again once the saga resolves.
    plant.belt.place(bag)


def test_committed_switch_cannot_be_rolled_back(plant):
    plant.belt.set("T1-CRCR1", 1)
    outcome = plant.switches.begin("T1-CRCR2", 2)
    with pytest.raises(ConflictError):
        plant.switches.rollback(outcome.switch_id)


def test_resume_of_a_committed_switch_is_rejected_without_writing(plant):
    plant.belt.set("T1-CRCR1", 1)
    outcome = plant.switches.begin("T1-CRCR2", 2)
    # The journal already proves the saga finished: resume is not a recovery
    # action anymore and is refused, committing nothing.
    before = plant.store.watermark
    with pytest.raises(ConflictError):
        plant.switches.resume(outcome.switch_id)
    assert plant.store.watermark == before


def test_concurrent_begins_serialise_so_the_loser_waits_then_is_rejected(plant):
    plant.belt.set("T1-CRCR1", 1)
    plant.switches.crash_after = "clear"
    import threading

    acquired = threading.Event()
    release = threading.Event()
    results: list[str] = []

    def first() -> None:
        with plant.switches._guarded():
            acquired.set()
            try:
                plant.switches.begin("T1-CRCR2", 2)
            except SwitchInterrupted:
                pass
            # Hold the lock until the contender has blocked on it.
            release.wait(5)
            results.append("interrupted")

    def contender() -> None:
        # Block until the first saga releases, then find the interrupted
        # switch it left behind and be refused - never interleaving.
        try:
            plant.switches.begin("T1-CRCR3", 3)
            results.append("committed")
        except ConflictError:
            results.append("rejected")

    holder = threading.Thread(target=first)
    waiter = threading.Thread(target=contender)
    holder.start()
    assert acquired.wait(5)
    waiter.start()
    assert waiter.join(0.2) is None and waiter.is_alive()
    release.set()
    holder.join(5)
    waiter.join(5)
    assert results == ["interrupted", "rejected"]
    assert len(plant.switches.pending()) == 1
    assert plant.switches.consistent() is True


def test_switch_with_an_empty_carousel_commits_cleanly(plant):
    plant.belt.set("T1-CRCR1", 1)
    outcome = plant.switches.begin("T1-CRCR2", 2)
    assert outcome.cleared == 0
    assert outcome.status == SWITCH_COMMITTED
    assert plant.switches.consistent() is True
