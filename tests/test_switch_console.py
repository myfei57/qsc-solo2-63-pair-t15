"""Console endpoints for crash-safe switch recovery."""

from __future__ import annotations

import json


def call(server, method, path, payload=None):
    response = server.call(method, path, payload)
    return response.status, json.loads(response.body)


def _belt_with_bags(server, count=2):
    status, flight = call(server, "POST", "/api/flights", {"code": "CA1234"})
    assert status == 201
    flight_id = flight["id"]
    for index in range(count):
        call(server, "POST", "/api/bags/checkin", {"barcode": f"BB{index}", "flight_id": flight_id})
    call(server, "POST", "/api/belt/set", {"carousel": "T1-CRCR1", "batch": 1})
    for bag in call(server, "GET", "/api/bags")[1]:
        call(server, "POST", "/api/belt/place", {"bag_id": bag["id"]})


def test_idle_belt_reports_no_active_switch(server):
    status, body = call(server, "GET", "/api/belt")
    assert status == 200
    assert body["switch"] == ""
    assert call(server, "GET", "/api/belt/switch")[1] == {"in_progress": False}
    assert call(server, "GET", "/api/belt/switch/decision")[1]["in_progress"] is False


def test_fault_then_continue_over_the_console(server):
    _belt_with_bags(server)
    status, body = call(
        server,
        "POST",
        "/api/belt/switch",
        {"carousel": "T1-CRCR2", "batch": 2, "crash_after": 1},
    )
    assert status == 409
    assert "interrupted" in body["error"]

    status, active = call(server, "GET", "/api/belt/switch")
    assert status == 200 and active["in_progress"] is True and active["steps_done"] == 1

    decision = call(server, "GET", "/api/belt/switch/decision")[1]
    assert decision["stuck_end"] == "new"
    assert len(decision["continue"]["finalises_loads"]) == 2

    status, settled = call(server, "POST", "/api/belt/switch/resume", {"decision": "continue"})
    assert status == 200
    assert settled["ok"] is True
    assert settled["active_switch"] == ""
    assert (settled["carousel"], settled["batch"]) == ("T1-CRCR2", 2)
    assert settled["switch"]["status"] == "completed"
    assert call(server, "GET", "/api/belt/switch/reconcile")[1]["ok"] is True


def test_fault_then_rollback_over_the_console(server):
    _belt_with_bags(server)
    call(
        server,
        "POST",
        "/api/belt/switch",
        {"carousel": "T1-CRCR2", "batch": 2, "crash_after": 1},
    )
    status, settled = call(server, "POST", "/api/belt/switch/resume", {"decision": "rollback"})
    assert status == 200
    assert settled["ok"] is True
    assert (settled["carousel"], settled["batch"]) == ("T1-CRCR1", 1)
    assert settled["switch"]["status"] == "rolled_back"
    # old batch restored with its bags, no load records left behind
    assert len(call(server, "GET", "/api/belt/on-carousel")[1]) == 2
    assert call(server, "GET", "/api/belt/loads")[1] == []


def test_switch_records_and_single_read_endpoint(server):
    _belt_with_bags(server, count=1)
    call(server, "POST", "/api/belt/switch", {"carousel": "T1-CRCR2", "batch": 2})
    records = call(server, "GET", "/api/belt/switch/records")[1]
    assert len(records) == 1 and records[0]["status"] == "completed"
    one = call(server, "GET", f"/api/belt/switch/{records[0]['id']}")[1]
    assert one["id"] == records[0]["id"]
    assert call(server, "GET", "/api/belt/switch/missing")[0] == 404
