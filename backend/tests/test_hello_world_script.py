"""Evidence used by the walking-skeleton checker (#72)."""

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
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
REVISION_EVENT = "69e38367-8c9f-4b99-8cf8-f1a7b8d1cca1"
PLAN = "2d673b19-3034-41ee-b64f-6211c28ef230"
FIRST = "3e22475e-66fb-4fdd-9194-2d7465ceddf7"
FIRST_TIME = "2026-11-20T08:00:00+00:00"
DELIVERY = "781f728c-73d8-4df8-997e-4f2e7087a502"
DECISION = "b973d23b-da9c-40da-8c6e-bc58dc00890a"
REVISED_DECISION = "e029954f-9f17-43a7-9372-96b7cb881ee6"
SNAPSHOT = "144647d6-aca1-4767-b7c3-26e95eec6c48"
OTHER_EVENT = "dc5741bc-6885-4466-b3a7-d8be3a023cbb"
CREATED = "evaluated: created, 1 replan request(s)"


def test_missing_docker_is_reported_as_an_unavailable_hop(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(hello_world.subprocess, "run", missing)
    assert hello_world.running_services() == set()
    with pytest.raises(RuntimeError, match="docker is unavailable"):
        hello_world.compose("run", "demo-feed")


def test_a_replan_request_alone_only_proves_the_connection():
    status, notes = hello_world.judge_hop8(CID, CREATED, None)

    assert status == "WIRED"
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
        "(1 replan request(s) not made: 1 Unavailable); will retry"
    )

    assert hello_world.judge_hop8(CID, message, None)[0] == "WIRED"


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
    status, notes = hello_world.judge_hop8(
        CID, CREATED, (200, True, EVENT), published_id=REVISION_EVENT, received=True
    )

    assert status == "PASS"
    assert notes[-1] == "listDisruptions shows it"


def test_resumed_request_can_pass_only_with_publication_and_receipt():
    message = "evaluated: duplicate, 1 replan request(s)"
    assert (
        hello_world.judge_hop8(CID, message, None, published_id=REVISION_EVENT)[0]
        == "WIRED"
    )
    assert (
        hello_world.judge_hop8(
            CID, message, None, published_id=REVISION_EVENT, received=True
        )[0]
        == "PASS"
    )


def test_a_200_listing_without_the_event_fails_the_hop():
    status, notes = hello_world.judge_hop8(
        CID, CREATED, (200, False, EVENT), published_id=REVISION_EVENT, received=True
    )

    assert status == "MISSING"
    assert notes[-1] == f"listDisruptions → 200 without event {EVENT}"


def test_a_failed_listing_fails_the_hop():
    status, notes = hello_world.judge_hop8(CID, CREATED, (503, False, EVENT))

    assert status == "MISSING"
    assert "503" in notes[-1]


def test_a_replay_needs_a_plan(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Invalid replay invoked Compose")

    monkeypatch.setattr(hello_world, "compose", unexpected)
    with pytest.raises(SystemExit) as stopped:
        hello_world.main(["--closure-event-id", EVENT])
    assert stopped.value.code == 2


@pytest.mark.parametrize("response", ["not JSON", json.dumps({"planId": PLAN})])
def test_broker_logs_alone_cannot_certify_a_pinned_revision(response, monkeypatch):
    hops = {n: hello_world.Hop(n, "test") for n in (4, 5, 8)}
    hops[5].set("WIRED")
    hops[8].set("PASS")
    request = {
        "scope": "simulation",
        "runId": OTHER_EVENT,
        "correlationId": CID,
        "planId": PLAN,
    }
    monkeypatch.setattr(hello_world, "compose", lambda *args: response)
    hello_world.check_saved_plan(hops, request, EVENT, REVISION_EVENT)
    assert [hop.status for hop in hops.values()] == ["MISSING", "WIRED", "WIRED"]
    assert "Could not verify committed C05 evidence" in hops[8].notes[-1]


def test_observed_event_must_match_the_saved_revision(monkeypatch):
    hops = {n: hello_world.Hop(n, "test") for n in (4, 5, 8)}
    hops[5].set("WIRED")
    hops[8].set("PASS")
    confirmed = {
        "ok": True,
        "detail": "confirmation",
        "version": 2,
        "decisionId": DECISION,
        "eventId": FIRST,
        "eventTime": FIRST_TIME,
    }
    saved = {
        "origin": "saved-delivery",
        "generation": {**confirmed, "version": 1, "deliveryId": DELIVERY},
        "confirmation": confirmed,
        "revision": {**confirmed, "version": 3, "eventId": REVISION_EVENT},
    }
    pending = {
        "ok": False,
        "detail": "awaiting publication",
        "pendingPublication": True,
    }
    pending_outcome = {"ok": False, "detail": "awaiting C07", "pendingOutcome": True}
    snapshots = iter(
        [
            {**saved, "confirmation": pending},
            {**saved, "revision": pending_outcome},
            saved,
        ]
    )
    monkeypatch.setattr(
        hello_world, "saved_plan_evidence", lambda *args: next(snapshots)
    )
    monkeypatch.setattr(hello_world.time, "sleep", lambda _: None)
    monkeypatch.setattr(hello_world, "wait_for_log", lambda *args: {"eventId": FIRST})
    hello_world.check_saved_plan(hops, {"planId": PLAN}, EVENT, OTHER_EVENT)
    assert hops[5].status == "PASS"
    assert hops[8].status == "WIRED"
    assert "matched" in hops[8].notes[-1]


@pytest.mark.parametrize(
    "mode",
    [
        "default",
        "pinned",
        "fixture",
        "replay",
        "wrong-closure",
        "submitted",
        "missing-handoff",
    ],
)
@pytest.mark.parametrize("matching_receipt", [True, False])
def test_checker_follows_one_dublin_trace_through_feed_logs_and_listing(
    mode, matching_receipt, monkeypatch, capsys
):
    scenario = json.loads(hello_world.SCENARIO_PATH.read_text(encoding="utf-8"))
    run_id, cid = scenario["runId"], scenario["correlationId"]
    argv = []
    pinned = mode != "default"
    submitted = mode in {"submitted", "missing-handoff"}
    if pinned:
        run_id, cid = "eb7dac64-be2f-4123-af8f-78d8f1e2ff1d", CID
        argv = ["--run-id", run_id, "--correlation-id", cid, "--plan-id", PLAN]
    replay = mode in {"replay", "wrong-closure"}
    selected_event = OTHER_EVENT if mode == "wrong-closure" else EVENT
    if replay:
        argv.extend(["--closure-event-id", selected_event])
    trace = {
        "scope": "simulation",
        "runId": run_id,
        "correlationId": cid,
        "eventId": EVENT,
    }

    def compose(*args, check=True):
        if "--query" in args:
            assert not pinned  # The standalone C09 probe cannot prove generation.
            assert args[-2:] == ("--correlation-id", cid)
            return f"query Ok: correlationId: {cid}"
        if "app.demo.c05_evidence" in args:
            assert pinned
            assert args[-8:] == (
                "--run-id",
                run_id,
                "--correlation-id",
                cid,
                "--plan-id",
                PLAN,
                "--closure-event-id",
                selected_event,
            )
            original = {"decisionId": DECISION, "deliveryId": DELIVERY}
            return json.dumps(
                {
                    "runId": run_id,
                    "correlationId": cid,
                    "planId": PLAN,
                    "origin": "c04-submission" if submitted else "saved-delivery",
                    "closureEventId": selected_event,
                    "generation": {
                        **original,
                        "ok": True,
                        "version": 1,
                        "detail": "saved proposal and C10 evidence",
                        "evidenceSnapshotIds": [SNAPSHOT],
                    },
                    "confirmation": {
                        **original,
                        "ok": True,
                        "version": 2,
                        "detail": "confirmed plan",
                        "localFixture": mode == "fixture",
                        "eventId": FIRST,
                        "eventTime": FIRST_TIME,
                    },
                    "revision": {"ok": False, "detail": "different closure"}
                    if mode == "wrong-closure"
                    else {
                        "ok": True,
                        "version": 3,
                        "deliveryId": DELIVERY,
                        "decisionId": REVISED_DECISION,
                        "detail": "committed revision",
                        "eventId": REVISION_EVENT,
                    },
                }
            )
        if "demo-feed" in args:
            assert not replay
            assert args[args.index("demo-feed") + 1 :] == (
                "--fresh",
                "--run-id",
                run_id,
                "--correlation-id",
                cid,
            )
            return f"queued event:   CityStateUpdated {EVENT}\n"
        if args[0] == "logs":
            if args[-1] == "api":
                archived = {
                    "component": "C04",
                    "scope": "simulation",
                    "runId": run_id,
                    "correlationId": cid,
                }
                saved = f"submitDelivery: delivery {DELIVERY} v1 persisted"
                handoff = (
                    f"submitDelivery: C05 generated plan {PLAN} "
                    f"v1 for delivery {DELIVERY}"
                )
                lines = [
                    {**archived, "message": saved},
                    {**archived, "message": handoff, "runId": OTHER_EVENT},
                    {**archived, "message": handoff.replace(DELIVERY, OTHER_EVENT)},
                ]
                if mode == "submitted":
                    lines.append({**archived, "message": handoff})
                return "\n".join(map(json.dumps, lines))
            if args[-1] != "c08-worker":
                assert ("--since" not in args) == replay
            if args[-1] == "c06-publisher":
                return json.dumps({**trace, "message": "published CityStateUpdated"})
            if args[-1] in {"c05-publisher", "c08-worker"}:
                revision = {
                    **trace,
                    "causationId": EVENT,
                    "eventId": REVISION_EVENT,
                    "eventType": "PlanReady",
                    "planId": PLAN,
                    "message": "published PlanReady",
                }
                if args[-1] == "c08-worker":
                    revision["message"] = "handled PlanReady: ack"
                    if not matching_receipt:
                        revision["eventId"] = CID
                # Another confirmation/revision can share the delivery trace.
                unrelated = {**revision, "eventId": CID, "causationId": None}
                others = [unrelated]
                if pinned:
                    # A single closure may revise two different plans in this run.
                    others.append({**revision, "eventId": EVENT, "planId": CID})
                    if args[-1] == "c08-worker":
                        # The first event predates the current checker invocation.
                        if FIRST_TIME in args:
                            others.append({**revision, "eventId": FIRST})
                            others.extend(
                                {
                                    **revision,
                                    "eventId": FIRST,
                                    "causationId": None,
                                    field: CID,
                                }
                                for field in ("runId", "planId")
                            )
                            if matching_receipt:
                                others.append(
                                    {**revision, "eventId": FIRST, "causationId": None}
                                )
                return "\n".join(map(json.dumps, [*others, revision]))
            assert args[-1] == "c07-worker"
            stale = {**trace, "message": "evaluated: no_disruption, 0 replan requests"}
            lines = [{**stale, field: "other"} for field in trace]
            lines.append(
                {
                    **trace,
                    "message": "evaluated: unknown, 0 replan request(s); will retry",
                }
            )
            lines.append({**trace, "message": CREATED})
            return "\n".join(json.dumps(line) for line in lines)
        return ""

    def urlopen(request, timeout):
        body = {}
        if request.selector == "/openapi.json":
            body = {
                "paths": {
                    "/api/v1/plans/{planId}/confirmation": {"post": {}},
                    "/api/v1/deliveries": {"post": {}},
                }
            }
        assert (
            request.method != "POST"
        )  # Rechecking a pinned plan never submits another delivery.
        if request.selector.startswith("/api/v1/disruptions"):
            assert request.selector == f"/api/v1/disruptions?runId={run_id}"
            assert request.get_header("Authorization") == "Bearer test-token"
            assert request.get_header("X-correlation-id") == cid
            body = {"disruptions": [{"triggerEventIds": [EVENT]}]}
        response = BytesIO(json.dumps(body).encode())
        response.status = 200
        response.headers = {}
        return response

    monkeypatch.setattr(hello_world, "compose", compose)
    monkeypatch.setattr(hello_world.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(hello_world, "TOKEN", "test-token")
    monkeypatch.setattr(hello_world, "WAIT_SECONDS", 0.02)
    monkeypatch.setattr(hello_world.time, "sleep", lambda _: None)
    hello_world.main(argv)
    output = capsys.readouterr().out
    assert f"runId={run_id} correlationId={cid}" in output
    assert (f"C09: query Ok: correlationId: {cid}" in output) == (not pinned)
    hop4 = next(
        line for line in output.splitlines() if "C04 → C05 generatePlan" in line
    )
    assert hop4.endswith(
        "PASS" if mode == "submitted" else "WIRED" if pinned else "MISSING"
    )
    hop5 = next(line for line in output.splitlines() if "C01 confirms the plan" in line)
    assert hop5.endswith(
        "PASS" if pinned and matching_receipt and mode != "fixture" else "WIRED"
    )
    assert ("The local CLI exercised C05" in output) == (
        mode == "fixture" and matching_receipt
    )
    if pinned:
        if submitted:
            hop3 = next(
                line for line in output.splitlines() if "C01 → C04 submit" in line
            )
            assert hop3.endswith("PASS")
        else:
            assert "Entry point: saved C04 delivery" in output
        assert all(
            field in output
            for field in [PLAN, DELIVERY, DECISION, SNAPSHOT, FIRST, "v1", "v2"]
        )
    assert (f"C08 received first PlanReady {FIRST}" in output) == (
        pinned and matching_receipt
    )
    assert ("listDisruptions shows it" in output) == (mode != "wrong-closure")
    hop8 = next(line for line in output.splitlines() if "C07 → C05 replan" in line)
    if mode == "wrong-closure":
        assert hop8.endswith("MISSING")
        assert "Checking recorded closure " + OTHER_EVENT in output
        return
    assert hop8.endswith("PASS" if matching_receipt else "WIRED")
    assert EVENT in output
    if pinned:
        assert REVISED_DECISION in output and "v3" in output
    if matching_receipt:
        assert f"revised PlanReady {REVISION_EVENT}; C08 received it" in output


def test_hop3_passes_when_delivery_saved_and_logged():
    status, notes = hello_world.judge_hop3(
        CID,
        201,
        {"deliveryId": DELIVERY},
        {
            "correlationId": CID,
            "message": f"submitDelivery: delivery {DELIVERY} v1 persisted",
        },
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


@pytest.mark.parametrize(
    ("response_headers", "expected"),
    [
        ({"X-Plan-Id": PLAN, "X-Plan-Version": "1", "X-Correlation-ID": CID}, "real"),
        ({"X-Plan-Id": PLAN, "X-Plan-Version": "1", "X-Plan-Stub": "true"}, "STUB"),
        (
            {"X-Plan-Id": PLAN, "X-Plan-Version": "1", "X-Correlation-ID": OTHER_EVENT},
            "MISSING",
        ),
        ({}, "MISSING"),
    ],
)
def test_signed_in_checker_submits_fresh_window_and_defers_closure(
    monkeypatch, capsys, response_headers, expected
):
    trace = {"scope": "simulation", "runId": OTHER_EVENT, "correlationId": CID}
    paths = {
        "/api/v1/deliveries": {"post": {}},
        "/api/v1/identity/me": {"get": {}},
        "/api/v1/plans/{planId}/confirmation": {"post": {}},
    }

    def urlopen(request, timeout):
        body = {}
        headers = {}
        status = 200
        if request.selector == "/openapi.json":
            body = {"paths": paths}
        elif request.selector == "/api/v1/identity/me":
            body = {"availableProviders": [{"providerId": FIRST}]}
        elif request.method == "POST":
            assert request.selector == f"/api/v1/deliveries?runId={OTHER_EVENT}"
            assert request.get_header("X-acting-provider") == FIRST
            assert request.get_header("X-correlation-id") == CID
            assert request.get_header("Authorization") == "Bearer signed-in"
            payload = json.loads(request.data)
            start = datetime.fromisoformat(payload["timeWindow"]["start"])
            assert (
                timedelta(minutes=29)
                < start - datetime.now(UTC)
                <= timedelta(minutes=30)
            )
            assert datetime.fromisoformat(
                payload["timeWindow"]["end"]
            ) - start == timedelta(hours=3)
            body, headers, status = {"deliveryId": DELIVERY}, response_headers, 201
        response = BytesIO(json.dumps(body).encode())
        response.status = status
        response.headers = headers
        return response

    def check(hops, selected, closure, revision, submitted):
        assert selected == {**trace, "planId": PLAN}
        assert submitted == DELIVERY and closure is None and revision is None
        hops[4].set("PASS")

    def compose(*args, **kwargs):
        assert "demo-feed" not in args
        if args[0] == "logs":
            assert args[-1] == "api"
            return json.dumps(
                {
                    **trace,
                    "component": "C04",
                    "message": f"submitDelivery: delivery {DELIVERY} v1 persisted",
                }
            )
        return ""

    monkeypatch.setattr(hello_world, "compose", compose)
    monkeypatch.setattr(hello_world.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(hello_world, "TOKEN", "signed-in")
    monkeypatch.setattr(hello_world, "check_saved_plan", check)
    hello_world.main(["--run-id", OTHER_EVENT, "--correlation-id", CID])
    output = capsys.readouterr().out
    assert "C04 saved delivery " + DELIVERY in output
    assert "Waiting for operator confirmation" in output
    if expected == "real":
        assert f"Confirm /plans?planId={PLAN}" in output
    else:
        assert f"Confirm /plans?planId={PLAN}" not in output
        assert next(
            line for line in output.splitlines() if "C04 → C05 generatePlan" in line
        ).endswith(expected)


@pytest.mark.parametrize(
    ("evidence", "expected", "words"),
    [
        ((True, "3", "accepted"), "PASS", "driver accepted"),
        ((True, "3", "allocated"), "WIRED", "v3 (allocated)"),
        ((True, "2", "offered"), "WIRED", "accept the driver offer"),
        ((True, "3", "unresolved"), "WIRED", "is unresolved"),
        ((True, "", ""), "WIRED", "no allocation"),
        ((False, "", ""), "WIRED", "Run demo-bind"),
    ],
)
def test_hop6_passes_only_once_the_driver_accepts(evidence, expected, words):
    status, note = hello_world.judge_hop6(*evidence)
    assert status == expected
    assert words in note


def test_allocation_evidence_reads_one_psql_row(monkeypatch):
    calls = []

    def compose(*args, **kwargs):
        calls.append(args)
        return "t|3|allocated\n"

    monkeypatch.setattr(hello_world, "compose", compose)
    plan = "6f777c7d-55ba-46fd-8a7c-60cdd766372b"
    assert hello_world.allocation_evidence(CID, plan) == (True, "3", "allocated")
    assert calls[0][:3] == ("exec", "-T", "db")
    assert plan in calls[0][-1] and CID in calls[0][-1]
