"""Stand-in smoke runner: drives the switch saga without pytest."""

import tempfile
import traceback
from pathlib import Path

from bagsort.app import Plant
from bagsort.belt.switching import InjectedFault
from bagsort.clock import Clock
from bagsort.errors import ConflictError, SwitchInterrupted
from bagsort.model.enums import BagState
from bagsort.store.store import Store


def build(root, fresh=True):
    path = str(root / "state.json")
    store = Store(path) if fresh else Store.open(path)
    return Plant.build(store, clock=Clock())


def check(cond, label):
    if not cond:
        raise AssertionError(label)
    print(f"  ok: {label}")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        plant = build(root)
        flight = plant.register_flight("CA1234", "CR1", "")
        bags = [plant.check_in(f"BAG{i:04d}", flight.id) for i in range(2)]

        # happy path
        plant.belt.set("T1-CRCR1", 1)
        plant.belt.place_batch(bags)
        plant.belt.append_transfer(flight.id, "T1-CRCR1", 1)
        outcome = plant.switches.begin("T1-CRCR2", 2)
        check(outcome.status == "committed", "happy path commits")
        check(plant.belt.carousel() == "T1-CRCR2", "belt announced new carousel")
        check(plant.belt.transfers() == [], "old commands retired")
        check(len(plant.belt.loads()) == 2, "two load records")
        check(plant.switches.consistent(), "reconcile clean after commit")
        check(plant.audit.verify(), "audit chain intact")

        # restart after commit
        plant.store.persist()
        plant2 = build(root, fresh=False)
        check(plant2.switches.consistent(), "restart reconcile clean")
        check(plant2.belt.carousel() == "T1-CRCR2", "restart belt state")

        # crash after clear, then resume after restart
        with tempfile.TemporaryDirectory() as tmp2:
            root2 = Path(tmp2)
            p = build(root2)
            f = p.register_flight("ZZ9", "CR1", "")
            bs = [p.check_in(f"X{i:04d}", f.id) for i in range(3)]
            p.belt.set("T1-CRCR1", 1)
            p.belt.place_batch(bs)
            p.switches.crash_after = "clear"
            try:
                p.switches.begin("T1-CRCR2", 2)
                raise AssertionError("expected interruption")
            except SwitchInterrupted as exc:
                sid = exc.switch_id
                check(exc.context["stranded_end"] == "new", "clear midpoint strands new side")
                check(exc.context["stranded_bags"] == [b.id for b in bs], "stranded bags named")
            check(p.switches.active().steps_done == ["clear"], "only clear marked")
            check(p.bag(bs[0].id).state is BagState.LOADED, "bag marked loaded")
            p.store.persist()

            pr = build(root2, fresh=False)
            active = pr.switches.active()
            check(active is not None and active.id == sid, "restart sees open switch")
            check(set(active.prior_bag_states) == {b.id for b in bs}, "prior states replayed")
            out = pr.switches.resume()
            check(out.status == "committed", "resume commits after restart")
            check(pr.belt.carousel() == "T1-CRCR2", "resume announced new")
            check(pr.switches.consistent(), "resume reconcile clean")
            check(pr.store.state().counters["belt.loaded"] == 3, "loaded counter 3")

        # crash after announce, rollback after restart
        with tempfile.TemporaryDirectory() as tmp3:
            root3 = Path(tmp3)
            p = build(root3)
            f = p.register_flight("YY8", "CR1", "")
            b = p.check_in("Y0001", f.id)
            prior_state = b.state
            p.belt.set("T1-CRCR1", 1)
            p.belt.place(b)
            p.belt.append_transfer(f.id, "T1-CRCR1", 1)
            p.switches.crash_after = "announce"
            try:
                p.switches.begin("T1-CRCR2", 2)
                raise AssertionError("expected interruption")
            except SwitchInterrupted as exc:
                sid = exc.switch_id
                check(exc.context["stranded_end"] == "old", "announce midpoint strands old side")
            p.store.persist()

            pr = build(root3, fresh=False)
            out = pr.switches.rollback()
            check(out.status == "rolled_back", "rollback status")
            check(out.restored == 1, "one bag restored")
            check(pr.belt.carousel() == "T1-CRCR1", "belt back at old carousel")
            check([x.id for x in pr.belt.on_carousel()] == [b.id], "bag back on carousel")
            check(pr.bag(b.id).state is prior_state, "bag state restored")
            check([c.batch for c in pr.belt.transfers()] == [1], "command restored")
            check([ld for ld in pr.belt.loads() if ld.switch_id == sid] == [], "load records withdrawn")
            check(pr.store.state().counters["belt.loaded"] == 0, "counter reversed")
            check(pr.switches.consistent(), "rollback reconcile clean")
            check(pr.audit.verify(), "audit intact after rollback")

        # crash during rollback, then continue rollback
        with tempfile.TemporaryDirectory() as tmp4:
            root4 = Path(tmp4)
            p = build(root4)
            f = p.register_flight("WW7", "CR1", "")
            b = p.check_in("W0001", f.id)
            prior_state = b.state
            p.belt.set("T1-CRCR1", 1)
            p.belt.place(b)
            p.belt.append_transfer(f.id, "T1-CRCR1", 1)
            p.switches.crash_after = "retire"
            try:
                p.switches.begin("T1-CRCR2", 2)
            except SwitchInterrupted:
                pass
            rec = p.switches.active()
            p.switches._compensate(rec, "retire")
            mid = p.switches.get(rec.id)
            check(mid.status == "rolling_back", "status rolling back mid rollback")
            check(mid.rollback_done == ["rb_retire"], "rb_retire marked only")
            out = p.switches.rollback()
            check(out.status == "rolled_back", "rollback finishes")
            check(out.rollback_done == ["rb_retire", "rb_announce", "rb_clear"], "compensations in order")
            check(p.switches.consistent(), "mid-rollback recovery reconciles")

        # guards
        with tempfile.TemporaryDirectory() as tmp5:
            root5 = Path(tmp5)
            p = build(root5)
            p.belt.set("T1-CRCR1", 1)
            p.switches.crash_after = "clear"
            try:
                p.switches.begin("T1-CRCR2", 2)
            except SwitchInterrupted:
                pass
            blocked = 0
            for fn in (
                lambda: p.belt.set("X", 9),
                lambda: p.switches.begin("Y", 3),
            ):
                try:
                    fn()
                except ConflictError:
                    blocked += 1
            check(blocked == 2, "mid-switch mutations blocked")
            try:
                p.switches.resume("nonexistent")
                raise AssertionError("missing switch should conflict")
            except ConflictError:
                pass

        # fail_in rolls the step transaction back
        with tempfile.TemporaryDirectory() as tmp6:
            root6 = Path(tmp6)
            p = build(root6)
            f = p.register_flight("VV6", "CR1", "")
            b = p.check_in("V0001", f.id)
            prior_state = b.state
            p.belt.set("T1-CRCR1", 1)
            p.belt.place(b)
            p.switches.fail_in = "announce"
            try:
                p.switches.begin("T1-CRCR2", 2)
            except InjectedFault:
                pass
            check(p.belt.carousel() == "T1-CRCR1", "announce rolled back on in-step fault")
            check(p.switches.active().steps_done == ["clear"], "clear persisted, announce not")
            p.switches.fail_in = ""
            out = p.switches.resume()
            check(out.status == "committed", "resume after in-step fault commits")

    print("ALL SMOKE CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
