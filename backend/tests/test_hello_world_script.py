"""How ``scripts/hello_world.py`` (#72) judges hop 8 from C07's log and C07's API."""

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "hello_world.py"
spec = importlib.util.spec_from_file_location("hello_world", SCRIPT)
hello_world = importlib.util.module_from_spec(spec)
sys.modules["hello_world"] = hello_world  # dataclasses look the module up
spec.loader.exec_module(hello_world)

CID = "7c4c48ca-b13e-41f3-8954-7cb1026be165"
EVENT = "edcb8146-d350-4ffd-98e3-d3396450033f"
CREATED = "evaluated: created, 1 replan request(s)"


def test_a_created_disruption_with_a_replan_request_counts_as_stub():
    status, notes = hello_world.judge_hop8(CID, CREATED, None)

    assert status == "STUB"
    assert CID in notes[0]


def test_a_disruption_without_any_replan_request_is_not_counted():
    status, notes = hello_world.judge_hop8(
        CID, "evaluated: created, 0 replan request(s)", None
    )

    assert status == "MISSING"
    assert "no replan request" in notes[0]


def test_a_retry_that_still_sent_one_request_counts():
    message = (
        "evaluated: created, 1 replan request(s) "
        "(1 replan request(s) pending: C05 unavailable); will retry"
    )

    assert hello_world.judge_hop8(CID, message, None)[0] == "STUB"


@pytest.mark.parametrize(
    "message",
    [
        None,
        "evaluated: no_disruption, 0 replan request(s)",
        "evaluated: unknown, 0 replan request(s) (C05 is unavailable)",
    ],
)
def test_no_disruption_or_no_log_is_missing(message):
    assert hello_world.judge_hop8(CID, message, None)[0] == "MISSING"


def test_listed_disruption_keeps_the_hop():
    status, notes = hello_world.judge_hop8(CID, CREATED, (200, True, EVENT))

    assert status == "STUB"
    assert notes[-1] == "listDisruptions shows it"


def test_a_200_listing_without_the_event_fails_the_hop():
    status, notes = hello_world.judge_hop8(CID, CREATED, (200, False, EVENT))

    assert status == "MISSING"
    assert notes[-1] == f"listDisruptions → 200 without event {EVENT}"


def test_a_failed_listing_fails_the_hop():
    status, notes = hello_world.judge_hop8(CID, CREATED, (503, False, EVENT))

    assert status == "MISSING"
    assert "503" in notes[-1]


def test_hop3_passes_when_delivery_saved_and_logged():
    status, notes = hello_world.judge_hop3(
        CID,
        201,
        {"deliveryId": "7c6b5a49-3827-4165-a0f9-e8d7c6b5a493"},
        {"correlationId": CID, "message": "submitDelivery: delivery persisted"},
    )

    assert status == "PASS"
    assert CID in notes[0]


def test_hop3_missing_when_http_fails_or_log_absent():
    assert hello_world.judge_hop3(CID, 503, None, None)[0] == "MISSING"
    assert (
        hello_world.judge_hop3(
            CID,
            201,
            {"deliveryId": "7c6b5a49-3827-4165-a0f9-e8d7c6b5a493"},
            None,
        )[0]
        == "MISSING"
    )
