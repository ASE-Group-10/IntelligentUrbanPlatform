#!/usr/bin/env python3
"""Walking-skeleton check (#72): is every hop of the hello-world path connected?

Run from the repository root with the Compose stack up (``docker compose up -d
--build``)::

    scripts/hello-world.sh                  # or: python3 scripts/hello_world.py
    ULOP_TOKEN=<Supabase access token> scripts/hello-world.sh

Each hop gets one status:

    PASS     exercised over its real connection on the selected trace
    STUB     exercised, but the component on the other side is a labelled stub
    WIRED    a connection is available, with only part of the hop verified
    SKIPPED  needs a signed-in token (set ULOP_TOKEN)
    MISSING  the required connection or evidence of the call is missing

The check uses runId and correlationId from fixtures/dublin-scenario.json.
Pass --run-id, --correlation-id and --plan-id to verify the stored proposal,
confirmation and revision for one saved delivery along with both C08 receipts.
Add --closure-event-id to recheck a completed trace using retained logs.
The script exits 1 unless every hop is PASS or STUB.

Standard library only, so it runs with any Python 3.11+.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

API = os.environ.get("ULOP_API", "http://127.0.0.1:8000").rstrip("/")
TOKEN = os.environ.get("ULOP_TOKEN")
SCENARIO_PATH = (
    Path(__file__).resolve().parents[1] / "fixtures" / "dublin-scenario.json"
)
WAIT_SECONDS = int(os.environ.get("ULOP_WAIT_SECONDS", "30"))
WORKERS = ("api", "c05-publisher", "c06-publisher", "c07-worker", "c08-worker")


@dataclass
class Hop:
    number: int
    name: str
    status: str = "MISSING"
    notes: list[str] = field(default_factory=list)

    def set(self, status: str, note: str | None = None) -> None:
        self.status = status
        if note:
            self.notes.append(note)


# ---- plumbing ---------------------------------------------------------------


def compose(*args: str, check: bool = True) -> str:
    try:
        result = subprocess.run(
            ["docker", "compose", *args], capture_output=True, text=True, check=False
        )
    except FileNotFoundError as error:
        if check:
            raise RuntimeError("docker is unavailable on this host") from error
        return ""
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"docker compose {args} failed")
    return result.stdout


def http(
    method: str, path: str, auth: bool = False, *, correlation_id: str | None = None
) -> tuple[int, object]:
    request = urllib.request.Request(f"{API}{path}", method=method)
    if correlation_id:
        request.add_header("X-Correlation-ID", correlation_id)
    if auth and TOKEN:
        request.add_header("Authorization", f"Bearer {TOKEN}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, None
    except (urllib.error.URLError, TimeoutError) as error:
        return 0, str(error)


def routes() -> set[tuple[str, str]]:
    """(METHOD, path) pairs the running API serves, from its /openapi.json."""
    status, spec = http("GET", "/openapi.json")
    if status != 200 or not isinstance(spec, dict):
        return set()
    return {
        (method.upper(), path)
        for path, operations in spec.get("paths", {}).items()
        for method in operations
    }


def consumers() -> dict[str, int]:
    """RabbitMQ queue name → number of consumers."""
    raw = compose(
        "exec",
        "-T",
        "rabbitmq",
        "rabbitmqctl",
        "list_queues",
        "name",
        "consumers",
        "--formatter",
        "json",
        check=False,
    )
    start = raw.find("[")
    if start < 0:
        return {}
    return {q["name"]: q["consumers"] for q in json.loads(raw[start:])}


def running_services() -> set[str]:
    raw = compose("ps", "--format", "json", check=False)
    services = set()
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("{"):
            item = json.loads(line)
            if item.get("State") == "running":
                services.add(item["Service"])
    return services


def log_lines(service: str, since: str | None) -> list[dict]:
    window = ["--since", since] if since is not None else []
    raw = compose("logs", "--no-log-prefix", *window, service, check=False)
    lines = []
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            lines.append(entry)
    return lines


def wait_for_log(
    service: str,
    since: str | None,
    trace: dict[str, str | None],
    needle: str,
    *,
    wait_for_retry: bool = False,
) -> dict | None:
    deadline = time.monotonic() + WAIT_SECONDS
    last_retry = None
    while True:
        for line in log_lines(service, since):
            if needle in line.get("message", "") and all(
                line.get(key) == value for key, value in trace.items()
            ):
                if wait_for_retry and line["message"].endswith("; will retry"):
                    # A redelivery can complete a request C05 could not yet take.
                    if not accepted_replan(line["message"]):
                        last_retry = line
                        continue
                return line
        if since is None or time.monotonic() >= deadline:
            break
        time.sleep(1)
    return last_retry


def has_consumer(queues: dict[str, int], prefix: str) -> bool:
    return any(name.startswith(prefix) and count > 0 for name, count in queues.items())


def feed_closure(run_id: str, cid: str) -> str:
    output = compose(
        "--profile",
        "demo",
        "run",
        "--rm",
        "-T",
        "demo-feed",
        "--fresh",
        "--run-id",
        run_id,
        "--correlation-id",
        cid,
    )
    event = re.search(
        r"^queued event:\s+CityStateUpdated ([0-9a-f-]{36})$", output, re.MULTILINE
    )
    if not event:
        raise RuntimeError("demo-feed printed no queued CityStateUpdated event ID")
    return event.group(1)


def c09_note(correlation_id: str) -> str:
    """Run the standalone C09 demo query (#85) as a diagnostic for hop 4."""
    try:
        out = compose(
            "exec",
            "-T",
            "api",
            "python",
            "-m",
            "app.components.c09_logistics_knowledge.demo_corpus",
            "--query",
            "--correlation-id",
            correlation_id,
        )
    except RuntimeError as error:
        return f"C09: demo corpus/query failed: {error}"
    query = [line for line in out.splitlines() if line.startswith("query ")]
    return f"C09: {query[-1] if query else 'no query result printed'}"


def saved_plan_evidence(trace: dict[str, str], closure_id: str | None) -> dict:
    """Read the separate C05 stages in one database snapshot."""
    args = [
        "exec",
        "-T",
        "api",
        "python",
        "-m",
        "app.demo.c05_evidence",
        "--run-id",
        trace["runId"],
        "--correlation-id",
        trace["correlationId"],
        "--plan-id",
        trace["planId"],
    ]
    if closure_id:
        args.extend(["--closure-event-id", closure_id])
    result = json.loads(compose(*args))
    expected = {
        "runId": trace["runId"],
        "correlationId": trace["correlationId"],
        "planId": trace["planId"],
        "closureEventId": closure_id,
        "origin": "saved-delivery",
    }
    if not isinstance(result, dict) or any(
        k not in result or result[k] != v for k, v in expected.items()
    ):
        raise ValueError(
            "C05 evidence does not identify the requested plan, run, trace and closure"
        )
    for name in ("generation", "confirmation", "revision"):
        phase = result.get(name)
        if not (
            isinstance(phase, dict)
            and isinstance(phase.get("ok"), bool)
            and isinstance(phase.get("detail"), str)
        ):
            raise ValueError(f"C05 evidence is missing its {name} result")
        if phase["ok"] and not all(
            phase.get(k) for k in ("version", "decisionId", "deliveryId")
        ):
            raise ValueError(f"C05 evidence is missing {name} identifiers")
    return result


def phase_note(phase: dict) -> str:
    if not phase["ok"]:
        return phase["detail"]
    fields = [f"v{phase['version']}", f"decision {phase['decisionId']}"]
    if snapshots := phase.get("evidenceSnapshotIds"):
        fields.append("C09 snapshots " + ", ".join(snapshots))
    if event := phase.get("eventId"):
        fields.append(f"outbox event {event}")
    return f"{phase['detail']} {', '.join(fields)}"


def check_saved_plan(
    hops: dict[int, Hop],
    trace: dict[str, str],
    closure_id: str | None,
    published_revision: str | None,
) -> None:
    try:
        deadline = time.monotonic() + WAIT_SECONDS
        while True:
            saved = saved_plan_evidence(trace, closure_id)
            # Delivery can precede the publication mark or C07 saving its response.
            pending = any(
                saved[phase].get("pendingPublication")
                for phase in ("confirmation", "revision")
            ) or saved["revision"].get("pendingOutcome")
            if not pending or time.monotonic() >= deadline:
                break
            time.sleep(1)
    except (RuntimeError, ValueError) as error:
        for n in (4, 5, 8):
            hops[n].notes.append(f"Could not verify committed C05 evidence: {error}")
        if hops[8].status == "PASS":
            hops[8].set("WIRED")
        return

    generated = saved["generation"]
    hops[4].set("WIRED" if generated["ok"] else "MISSING", phase_note(generated))
    if generated["ok"]:
        hops[4].notes.append(
            f"Entry point: saved C04 delivery {generated['deliveryId']}, "
            f"plan {trace['planId']}. HTTP submission is unverified."
        )

    confirmed = saved["confirmation"]
    hops[5].notes.append(phase_note(confirmed))
    if confirmed["ok"] and confirmed.get("eventId") and confirmed.get("eventTime"):
        # Confirmation predates the checker. The DB outbox creation time precedes
        # publication and receipt, even if the workers used a scenario clock.
        first = {
            **trace,
            "eventId": confirmed["eventId"],
            "eventType": "PlanReady",
            "causationId": None,
        }
        receipt = wait_for_log(
            "c08-worker", confirmed["eventTime"], first, "handled PlanReady: ack"
        )
        if receipt:
            hops[5].notes.append(f"C08 received first PlanReady {confirmed['eventId']}")
            if hops[5].status == "WIRED":
                hops[5].set("PASS")
        else:
            hops[5].notes.append(
                f"No C08 receipt found for first PlanReady {confirmed['eventId']}"
            )

    revision = saved["revision"]
    hops[8].notes.append(phase_note(revision))
    if hops[8].status == "PASS" and not (
        revision["ok"] and revision.get("eventId") == published_revision
    ):
        hops[8].set(
            "WIRED",
            "The observed event could not be matched to the committed C05 revision.",
        )


# ---- the hops ---------------------------------------------------------------

_EVALUATED = re.compile(r"evaluated: (\w+), (\d+) replan request")


def accepted_replan(message: str) -> bool:
    match = _EVALUATED.search(message)
    # C07 reports "duplicate" when a redelivery resumes a pending replan.
    return bool(match and match[1] in {"created", "duplicate"} and int(match[2]) > 0)


def judge_hop8(
    cid: str,
    message: str | None,
    listing: tuple[int, bool, str] | None,
    *,
    published_id: str | None = None,
    received: bool = False,
) -> tuple[str, list[str]]:
    """Count hop 8 when a replan produced a published and received revision."""
    if message is None:
        return "MISSING", [f"C07 didn't evaluate {cid} within {WAIT_SECONDS}s"]
    if not accepted_replan(message):
        return "MISSING", [f"C07 {message} for {cid}: no replan request reached C05"]
    status, notes = "WIRED", [f"C07 {message} for {cid}"]
    if published_id and received:
        status = "PASS"
        notes.append(f"C05 published revised PlanReady {published_id}; C08 received it")
    elif published_id:
        notes.append(
            f"C05 published revised PlanReady {published_id}; C08 receipt pending"
        )
    else:
        notes.append("No revised PlanReady was observed for this closure")
    if listing is not None:
        http_status, listed, event_id = listing
        if listed:
            notes.append("listDisruptions shows it")
        else:
            status = "MISSING"
            notes.append(f"listDisruptions → {http_status} without event {event_id}")
    return status, notes


def uuid4_arg(value: str) -> str:
    parsed = UUID(value)
    if parsed.version != 4:
        raise argparse.ArgumentTypeError("expected a UUIDv4")
    return str(parsed)


def main(argv: list[str] | None = None) -> int:
    scenario = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--run-id",
        type=uuid4_arg,
        default=scenario["runId"],
        help="simulation run UUIDv4 (default: Dublin fixture)",
    )
    parser.add_argument(
        "--correlation-id",
        type=uuid4_arg,
        default=scenario["correlationId"],
        help="delivery trace UUIDv4 (default: Dublin fixture)",
    )
    parser.add_argument(
        "--plan-id",
        type=uuid4_arg,
        help="verify the proposal, confirmation and closure revision of this plan",
    )
    parser.add_argument(
        "--closure-event-id",
        type=uuid4_arg,
        help="verify a recorded closure without injecting one (requires --plan-id)",
    )
    args = parser.parse_args(argv)
    if args.closure_event_id and not args.plan_id:
        parser.error("--closure-event-id requires --plan-id")
    run_id, cid = args.run_id, args.correlation_id
    print(f"Dublin simulation runId={run_id} correlationId={cid}", flush=True)
    hops = {
        n: Hop(n, name)
        for n, name in enumerate(
            [
                "Backend app + workers",
                "C01 sign-in → C02 /identity/me",
                "C01 → C03 register a resource",
                "C01 → C04 submit a delivery",
                "C04 → C05 generatePlan → C09 + C10",
                "C01 confirms the plan → C05 PlanReady",
                "C08 consumes PlanReady, driver accepts",
                "Feed → C06 → CityStateUpdated",
                "C07 → C05 replan → revised PlanReady",
                "C11 consumes events → KPI screen",
                "C12 records and replays",
            ]
        )
    }

    # Hop 0: the API answers and every worker container is running.
    status, health = http("GET", "/health")
    running = running_services()
    down = [s for s in WORKERS if s not in running]
    if status == 200 and not down:
        hops[0].set("PASS", f"API healthy at {API}; {', '.join(WORKERS[1:])} running")
    else:
        hops[0].set(
            "MISSING",
            f"/health → {status or health}; not running: {', '.join(down) or 'none'}. "
            "Start the stack with `docker compose up -d --build`",
        )
    served = routes()
    queues = consumers()

    # Hop 1: C02 identity over HTTP with a bearer token.
    if ("GET", "/api/v1/identity/me") not in served:
        hops[1].set("MISSING", "GET /api/v1/identity/me isn't served")
    elif not TOKEN:
        hops[1].set("SKIPPED", "route is up; set ULOP_TOKEN to call it signed in")
    else:
        status, profile = http(
            "GET", "/api/v1/identity/me", auth=True, correlation_id=cid
        )
        if status == 200 and isinstance(profile, dict):
            hops[1].set("PASS", f"signed in as {profile.get('displayName')}")
        else:
            hops[1].set("MISSING", f"/identity/me → {status}")

    # Hop 2: C03 resources.
    if ("POST", "/api/v1/resources") not in served:
        hops[2].set("MISSING", "POST /api/v1/resources isn't served")
    elif not TOKEN:
        hops[2].set("SKIPPED", "route is up; set ULOP_TOKEN to call it signed in")
    else:
        hops[2].set("WIRED", "route is up; registering needs a provider, not done here")

    # Hops 3 and 5 use HTTP. Hop 4 starts inside the C04 submit handler.
    probes = {
        3: ("POST", "/api/v1/deliveries", "C04 submitDelivery"),
        5: ("POST", "/api/v1/plans/{planId}/confirmation", "C05 confirmPlan"),
    }
    for n, (method, path, what) in probes.items():
        if (method, path) in served:
            hops[n].set("WIRED", f"{what} endpoint is served")
        else:
            hops[n].set("MISSING", f"{what} ({method} {path}) isn't served")
    if not args.plan_id:
        hops[4].set(
            "MISSING",
            "C04 submission has yet to exercise C05 generation in this check. "
            "Run app.demo.c05 to exercise C05 from a saved delivery.",
        )
        hops[4].notes.append(c09_note(cid))

    # Hop 6: C08 consumes PlanReady and serves the assignment routes.
    if (
        has_consumer(queues, "c08.")
        and ("GET", "/api/v1/vehicle-assignments") in served
    ):
        hops[6].set(
            "WIRED",
            "c08.plan-ready has a consumer and the assignment API is up; "
            "this check does not confirm a plan or verify its allocation",
        )
    else:
        hops[6].set("MISSING", "no c08.plan-ready consumer or assignment API")

    # Hops 7–8: follow this observation even when the fixture trace is reused.
    event_id, published_id = args.closure_event_id, None
    since = None
    if event_id:
        print(f"Checking recorded closure {event_id}", flush=True)
    else:
        since = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            event_id = feed_closure(run_id, cid)
        except RuntimeError as error:
            hops[7].set("MISSING", str(error))
    if event_id:
        trace = {
            "scope": "simulation",
            "runId": run_id,
            "correlationId": cid,
            "eventId": event_id,
        }
        published = wait_for_log(
            "c06-publisher", since, trace, "published CityStateUpdated"
        )
        if published:
            hops[7].set(
                "PASS",
                f"correlationId {cid}: C06 published CityStateUpdated {event_id}",
            )
        else:
            hops[7].set("MISSING", f"C06 didn't publish {cid} within {WAIT_SECONDS}s")

        evaluated = wait_for_log(
            "c07-worker", since, trace, "evaluated", wait_for_retry=True
        )
        listing = None
        if evaluated and TOKEN:
            status, body = http(
                "GET",
                f"/api/v1/disruptions?runId={run_id}",
                auth=True,
                correlation_id=cid,
            )
            listed = isinstance(body, dict) and any(
                event_id in d.get("triggerEventIds", [])
                for d in body.get("disruptions", [])
            )
            listing = (status, listed, event_id)
        message = evaluated["message"] if evaluated else None
        received = False
        if message and accepted_replan(message):
            revision_trace = {
                "scope": "simulation",
                "runId": run_id,
                "correlationId": cid,
                "causationId": event_id,
                "eventType": "PlanReady",
            }
            if args.plan_id:
                revision_trace["planId"] = args.plan_id
            revision = wait_for_log(
                "c05-publisher", since, revision_trace, "published PlanReady"
            )
            if revision and (published_id := revision.get("eventId")):
                received = (
                    wait_for_log(
                        "c08-worker",
                        since,
                        {**revision_trace, "eventId": published_id},
                        "handled PlanReady: ack",
                    )
                    is not None
                )
        status8, notes8 = judge_hop8(
            cid, message, listing, published_id=published_id, received=received
        )
        hops[8].set(status8)
        hops[8].notes.extend(notes8)

    if args.plan_id:
        check_saved_plan(
            hops,
            {
                "scope": "simulation",
                "runId": run_id,
                "correlationId": cid,
                "planId": args.plan_id,
            },
            event_id,
            published_id,
        )

    # Hops 9–10: C11 and C12 consume events and serve their screens' routes.
    for n, prefix, route, what in (
        (9, "c11.", ("POST", "/api/v1/analytics/kpis/query"), "C11"),
        (10, "c12.", ("GET", "/api/v1/simulation-runs"), "C12"),
    ):
        consumer, served_route = has_consumer(queues, prefix), route in served
        if consumer and served_route:
            hops[n].set("WIRED", f"{what} consumer and {route[0]} {route[1]} are up")
        else:
            missing = [
                m
                for m, ok in (
                    (f"{prefix}* consumer", consumer),
                    (f"{route[0]} {route[1]}", served_route),
                )
                if not ok
            ]
            hops[n].set("MISSING", f"{what}: no {' and no '.join(missing)}")

    return report(list(hops.values()))


def report(hops: list[Hop]) -> int:
    width = max(len(h.name) for h in hops)
    print(f"\nHello-world path (#72) against {API}\n")
    for hop in hops:
        print(f"  {hop.number:>2}  {hop.name:<{width}}  {hop.status}")
        for note in hop.notes:
            print(f"      {'':<{width}}  {note}")
    counts = {
        s: sum(h.status == s for h in hops)
        for s in ("PASS", "STUB", "WIRED", "SKIPPED", "MISSING")
    }
    print("\n  " + ", ".join(f"{n} {s.lower()}" for s, n in counts.items() if n))
    done = counts["PASS"] + counts["STUB"]
    print(f"  {done} of {len(hops)} hops exercised end to end\n")
    return 0 if done == len(hops) else 1


if __name__ == "__main__":
    sys.exit(main())
