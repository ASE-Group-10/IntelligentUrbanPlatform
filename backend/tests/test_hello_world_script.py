"""Evidence used by the walking-skeleton checker (#72)."""

import importlib.util
import json
import sys
from io import BytesIO
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


def test_missing_docker_is_reported_as_an_unavailable_hop(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(hello_world.subprocess, "run", missing)
    assert hello_world.running_services() == set()
    with pytest.raises(RuntimeError, match="docker is unavailable"):
        hello_world.compose("run", "demo-feed")


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


@pytest.mark.parametrize("override_ids", [False, True])
def test_checker_follows_one_dublin_trace_through_feed_logs_and_listing(
    override_ids, monkeypatch, capsys
):
    scenario = json.loads(hello_world.SCENARIO_PATH.read_text(encoding="utf-8"))
    run_id, cid = scenario["runId"], scenario["correlationId"]
    argv = []
    if override_ids:
        run_id, cid = "eb7dac64-be2f-4123-af8f-78d8f1e2ff1d", CID
        argv = ["--run-id", run_id, "--correlation-id", cid]
    trace = {
        "scope": "simulation",
        "runId": run_id,
        "correlationId": cid,
        "eventId": EVENT,
    }

    def compose(*args, check=True):
        if "--query" in args:
            assert args[-2:] == ("--correlation-id", cid)
            return f"query Ok: correlationId: {cid}"
        if "demo-feed" in args:
            assert args[args.index("demo-feed") + 1 :] == (
                "--fresh",
                "--run-id",
                run_id,
                "--correlation-id",
                cid,
            )
            return f"queued event:   CityStateUpdated {EVENT}\n"
        if args[0] == "logs":
            if args[-1] == "c06-publisher":
                return json.dumps({**trace, "message": "published CityStateUpdated"})
            assert args[-1] == "c07-worker"
            stale = {**trace, "message": "evaluated: no_disruption, 0 replan requests"}
            lines = [{**stale, field: "other"} for field in trace]
            lines.append({**trace, "message": CREATED})
            return "\n".join(json.dumps(line) for line in lines)
        return ""

    def urlopen(request, timeout):
        body = {}
        if request.selector == "/openapi.json":
            body = {"paths": {"/api/v1/plans": {"post": {}}}}
        if request.selector.startswith("/api/v1/disruptions"):
            assert request.selector == f"/api/v1/disruptions?runId={run_id}"
            assert request.get_header("Authorization") == "Bearer test-token"
            assert request.get_header("X-correlation-id") == cid
            body = {"disruptions": [{"triggerEventIds": [EVENT]}]}
        response = BytesIO(json.dumps(body).encode())
        response.status = 200
        return response

    monkeypatch.setattr(hello_world, "compose", compose)
    monkeypatch.setattr(hello_world.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(hello_world, "TOKEN", "test-token")
    hello_world.main(argv)
    output = capsys.readouterr().out
    assert f"runId={run_id} correlationId={cid}" in output
    assert f"C09: query Ok: correlationId: {cid}" in output
    hop4 = next(
        line for line in output.splitlines() if "C04 → C05 generatePlan" in line
    )
    assert hop4.endswith("MISSING")
    assert "listDisruptions shows it" in output
