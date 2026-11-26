"""Local-only real C04 HTTP -> outbox -> RabbitMQ -> C11/C12 verification.

Never publishes fixtures or calls consumer handlers. Run on a fresh local stack.
The optional test-identity seed is only for the isolated Compose CI fixture issuer.
"""

import argparse
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select, text

from app.components.analytics_verify import LocalAPI, require, verify_http
from app.components.c11_kpi_analytics.store import observations
from app.components.c12_simulation_replay.persistence import runs
from app.components.c12_simulation_replay.recording import CaptureRecorder, captures
from app.config import get_settings
from app.infrastructure.database import make_engine
from app.shared.contracts import EventEnvelope


def local_engine():
    require(os.environ.get("ULOP_LOCAL_TRACE") == "1", "Use a local trace operator")
    engine = make_engine(get_settings().database_url.get_secret_value())
    require(
        engine.url.host in {"db", "127.0.0.1", "localhost"}
        and engine.url.database == "ulop",
        "Trace operator requires the local Compose database",
    )
    return engine


def saved_events(engine, run_id, cid, provider_id):
    with engine.connect() as conn:
        archive = conn.execute(
            select(runs.c.archive).where(
                runs.c.run_id == run_id,
                runs.c.provider_id == provider_id,
            )
        ).scalar_one_or_none()
        require(archive is not None, "No sealed provider recording for this run")
        require(
            archive["manifest"]["status"] == "incomplete", "Unexpected capture scope"
        )
        events = [
            EventEnvelope.model_validate(s["record"]["event"]) for s in archive["steps"]
        ]
        require(bool(events), "An empty recording cannot pass a trace")
        require(
            all(
                e.producer == "C04"
                and e.scope == "simulation"
                and e.run_id == run_id
                and e.correlation_id == cid
                and e.payload.get("providerId") == str(provider_id)
                for e in events
            ),
            "Recording does not match the selected C04/provider/trace",
        )
        source = (
            conn.execute(
                text(
                    "select event_id, envelope, published_at "
                    "from c04_delivery_management.outbox "
                    "where scope_key = :scope and correlation_id = :cid order by seq"
                ),
                {"scope": f"simulation:{run_id}", "cid": cid},
            )
            .mappings()
            .all()
        )
        require(
            [r["event_id"] for r in source] == [e.event_id for e in events]
            and all(r["published_at"] is not None for r in source)
            and [EventEnvelope.model_validate(r["envelope"]) for r in source] == events,
            "Recorded outcomes do not match broker-confirmed C04 outbox rows",
        )
    return archive, events


def frozen_state(engine, run_id):
    # Test instrumentation only: compare owned operational/recorded rows. This is
    # not a business adapter reading another component's tables for its logic.
    with engine.connect() as conn:
        return [
            conn.execute(
                text(
                    f"select row_to_json(t)::text as body from {table} t order by body"
                )
            )
            .scalars()
            .all()
            for table in (
                "c04_delivery_management.deliveries",
                "c08_allocation.allocations",
                "c12_simulation_replay.captures",
                "c12_simulation_replay.runs",
            )
        ]


def verify(engine, api, run_id, cid, provider_id):
    archive, events = saved_events(engine, run_id, cid, provider_id)
    before = frozen_state(engine, run_id)
    result = verify_http(
        api,
        provider_id,
        run_id,
        events,
        datetime.now(UTC),
        period=archive["manifest"]["available_ranges"][0],
    )
    require(frozen_state(engine, run_id) == before, "Playback changed live/frozen data")
    return {
        "runId": str(run_id),
        "correlationId": str(cid),
        "providerId": str(provider_id),
        "hops": {"9": "PASS", "10": "PASS"},
        "recordingScope": "C04 delivery outcomes only",
        "recordingStatus": "incomplete",
        "replay": result,
        "identity": "test issuer"
        if os.environ.get("ULOP_TEST_IDENTITY") == "1"
        else "configured C02 issuer",
    }


def seed_identity(engine, principal, provider):
    require(os.environ.get("ULOP_TEST_IDENTITY") == "1", "Test identity seed disabled")
    with engine.begin() as conn:
        conn.execute(
            text(
                "insert into c02_identity_access.profiles "
                "values (:id, 'Compose test actor', false)"
            ),
            {"id": principal},
        )
        conn.execute(
            text(
                "insert into c02_identity_access.providers "
                "values (:id, 'Compose test provider')"
            ),
            {"id": provider},
        )
        for role in ("operator", "analyst"):
            conn.execute(
                text(
                    "insert into c02_identity_access.provider_memberships "
                    "values (:id,:pid,:role)"
                ),
                {"id": principal, "pid": provider, "role": role},
            )


def submit_and_capture(engine, api, run_id, cid, provider_id):
    # Check the actual HTTPS/JWT-backed C02 identity before any operator writes.
    identity = api("GET", "/api/v1/identity/me")
    require(
        any(
            m["providerId"] == str(provider_id) and "analyst" in m["roles"]
            for m in identity["memberships"]
        ),
        "An authorised analyst is required",
    )
    anchor = datetime.now(UTC)
    recorder = CaptureRecorder(engine)
    recorder.begin(
        run_id,
        provider_id,
        anchor - timedelta(minutes=2),
        {"initialState": {"deliveries": {}}, "source": "real local C04 HTTP"},
        True,
    )
    # The request is synthetic; the producer, transports and consumers are real.
    body = json.loads(
        (
            Path(__file__).resolve().parents[3]
            / "contracts/components/c04-delivery-management/examples"
            / "submit-delivery-request.json"
        ).read_text()
    )
    departure = anchor + timedelta(minutes=30)
    body["timeWindow"] = {
        "start": departure.isoformat(),
        "end": (departure + timedelta(hours=3)).isoformat(),
    }
    body["idempotencyKey"] = "compose-http-" + str(uuid4())
    delivery = api("POST", f"/api/v1/deliveries?runId={run_id}", body)
    require(bool(delivery.get("deliveryId")), "C04 returned no saved delivery")
    deadline = time.monotonic() + 40
    while True:
        with engine.connect() as conn:
            source = (
                conn.execute(
                    text(
                        "select event_id, published_at "
                        "from c04_delivery_management.outbox "
                        "where scope_key=:scope and correlation_id=:cid order by seq"
                    ),
                    {"scope": f"simulation:{run_id}", "cid": cid},
                )
                .mappings()
                .all()
            )
            capture = conn.execute(
                select(captures.c.body).where(captures.c.run_id == run_id)
            ).scalar_one()
            ids = [r["event_id"] for r in source]
            observed = (
                set(
                    conn.execute(
                        select(observations.c.event_id).where(
                            observations.c.event_id.in_(ids)
                        )
                    ).scalars()
                )
                if ids
                else set()
            )
        require(not capture.get("invalid"), "C12 rejected a C04 outcome")
        if (
            ids
            and all(r["published_at"] for r in source)
            and observed == set(ids)
            and [UUID(e["event_id"]) for e in capture["events"]] == ids
        ):
            break
        require(
            time.monotonic() < deadline, "C04/C11/C12 evidence missing before timeout"
        )
        time.sleep(0.2)
    recorder.seal(run_id, anchor + timedelta(minutes=1))
    return verify(engine, api, run_id, cid, provider_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("run", "verify", "seed-identity"))
    parser.add_argument("--run-id", type=UUID, required=True)
    parser.add_argument("--correlation-id", type=UUID, required=True)
    parser.add_argument("--provider-id", type=UUID, required=True)
    parser.add_argument("--principal-id", type=UUID)
    args = parser.parse_args()
    engine = local_engine()
    try:
        if args.operation == "seed-identity":
            require(args.principal_id is not None, "Supply the test principal")
            seed_identity(engine, args.principal_id, args.provider_id)
            return
        api = LocalAPI(
            "http://127.0.0.1:8000",
            os.environ.get("ULOP_TOKEN", ""),
            args.provider_id,
            args.correlation_id,
        )
        operation = submit_and_capture if args.operation == "run" else verify
        print(
            json.dumps(
                operation(
                    engine, api, args.run_id, args.correlation_id, args.provider_id
                )
            )
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
