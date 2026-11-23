#!/usr/bin/env python3
"""Walking-skeleton check (#72): is every hop of the hello-world path connected?

Run from the repository root with the Compose stack up (``docker compose up -d
--build``)::

    scripts/hello-world.sh                  # or: python3 scripts/hello_world.py
    ULOP_TOKEN=<Supabase access token> scripts/hello-world.sh

Each hop gets one status:

    PASS     exercised in this run over its real connection
    STUB     exercised, but the component on the other side is a labelled stub
    WIRED    its route or queue consumer is up, but nothing in this run reached it
    SKIPPED  needs a signed-in token (set ULOP_TOKEN)
    MISSING  its route or consumer doesn't exist yet

The closure from ``demo-feed`` carries one correlationId through hops 7 and 8.
Once C04 and C05 exist, hop 3 starts the run and that correlationId should
reach every hop. The script exits 1 unless every hop is PASS or STUB.

Standard library only, so it runs with any Python 3.11+.
"""

from __future__ import annotations

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

API = os.environ.get("ULOP_API", "http://127.0.0.1:8000").rstrip("/")
TOKEN = os.environ.get("ULOP_TOKEN")
DEMO_RUN = "c3f1a2b4-5d6e-4f70-8a9b-0c1d2e3f4a5b"
WAIT_SECONDS = int(os.environ.get("ULOP_WAIT_SECONDS", "30"))
WORKERS = ("api", "c06-publisher", "c07-worker", "c08-worker")


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
    result = subprocess.run(
        ["docker", "compose", *args], capture_output=True, text=True, check=False
    )
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"docker compose {args} failed")
    return result.stdout


def http(method: str, path: str, auth: bool = False) -> tuple[int, object]:
    request = urllib.request.Request(f"{API}{path}", method=method)
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


def log_lines(service: str, since: str) -> list[dict]:
    raw = compose("logs", "--no-log-prefix", "--since", since, service, check=False)
    lines = []
    for line in raw.splitlines():
        try:
            lines.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return lines


def wait_for_log(
    service: str, since: str, correlation_id: str, needle: str
) -> dict | None:
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        for line in log_lines(service, since):
            if line.get("correlationId") == correlation_id and needle in line.get(
                "message", ""
            ):
                return line
        time.sleep(1)
    return None


def has_consumer(queues: dict[str, int], prefix: str) -> bool:
    return any(name.startswith(prefix) and count > 0 for name, count in queues.items())


# ---- the hops ---------------------------------------------------------------

_EVALUATED = re.compile(r"evaluated: (\w+), (\d+) replan request")


def judge_hop8(
    cid: str, message: str | None, listing: tuple[int, bool, str] | None
) -> tuple[str, list[str]]:
    """Hop 8 counts only if C07 created a disruption, sent C05 at least one
    replan request for this correlationId and, when checked, lists it over HTTP.

    ``message`` is C07's ``evaluated: …`` log line (``None`` if it never came);
    ``listing`` is ``(HTTP status, event listed?, event ID)`` when ULOP_TOKEN
    let the script call ``listDisruptions``.
    """
    if message is None:
        return "MISSING", [f"C07 didn't evaluate {cid} within {WAIT_SECONDS}s"]
    match = _EVALUATED.search(message)
    if not match or match.group(1) != "created":
        return "MISSING", [f"C07 {message} for {cid} (no disruption)"]
    if int(match.group(2)) < 1:
        return "MISSING", [f"C07 {message} for {cid}: no replan request reached C05"]
    status, notes = (
        "STUB",
        [f"C07 {message} for {cid}; C05 replan is the labelled stub until #59"],
    )
    if listing is not None:
        http_status, listed, event_id = listing
        if listed:
            notes.append("listDisruptions shows it")
        else:
            status = "MISSING"
            notes.append(f"listDisruptions → {http_status} without event {event_id}")
    return status, notes


def main() -> int:
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
        status, profile = http("GET", "/api/v1/identity/me", auth=True)
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

    # Hops 3–5: C04 and C05 over HTTP (C09/C10 are in-process inside C05).
    probes = {
        3: ("POST", "/api/v1/deliveries", "C04 submitDelivery"),
        4: ("POST", "/api/v1/plans", "C05 generatePlan"),
        5: ("POST", "/api/v1/plans/{planId}/confirmation", "C05 confirmPlan"),
    }
    for n, (method, path, what) in probes.items():
        if (method, path) in served:
            hops[n].set("WIRED", f"{what} is served; this script doesn't call it yet")
        else:
            hops[n].set("MISSING", f"{what} ({method} {path}) isn't served")

    # Hop 6: C08 consumes PlanReady and serves the assignment routes.
    if (
        has_consumer(queues, "c08.")
        and ("GET", "/api/v1/vehicle-assignments") in served
    ):
        hops[6].set(
            "WIRED",
            "c08.plan-ready has a consumer and the assignment API is up; "
            "no C05 PlanReady reaches it in this run",
        )
    else:
        hops[6].set("MISSING", "no c08.plan-ready consumer or assignment API")

    # Hops 7–8: inject the demo closure and follow its correlationId.
    since = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        feed = compose("--profile", "demo", "run", "--rm", "-T", "demo-feed", "--fresh")
    except RuntimeError as error:
        feed = ""
        hops[7].set("MISSING", f"demo-feed failed: {error}")
    correlation = re.search(r"correlationId:\s+(\S+)", feed)
    event = re.search(r"queued event:\s+CityStateUpdated (\S+)", feed)
    if correlation:
        cid = correlation.group(1)
        published = wait_for_log(
            "c06-publisher", since, cid, "published CityStateUpdated"
        )
        if published:
            hops[7].set("PASS", f"correlationId {cid}: C06 published CityStateUpdated")
        else:
            hops[7].set("MISSING", f"C06 didn't publish {cid} within {WAIT_SECONDS}s")

        evaluated = wait_for_log("c07-worker", since, cid, "evaluated")
        listing = None
        if evaluated and TOKEN and event:
            status, body = http(
                "GET", f"/api/v1/disruptions?runId={DEMO_RUN}", auth=True
            )
            listed = isinstance(body, dict) and any(
                event.group(1) in d.get("triggerEventIds", [])
                for d in body.get("disruptions", [])
            )
            listing = (status, listed, event.group(1))
        status8, notes8 = judge_hop8(
            cid, evaluated["message"] if evaluated else None, listing
        )
        hops[8].set(status8)
        hops[8].notes.extend(notes8)
    elif not hops[7].notes:
        hops[7].set("MISSING", "demo-feed printed no correlationId")

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
