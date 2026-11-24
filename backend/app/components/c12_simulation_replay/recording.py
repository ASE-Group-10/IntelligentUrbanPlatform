"""C04-only simulation capture, explicitly incomplete for the whole platform."""

import hashlib
import json
from datetime import datetime
from uuid import uuid4

from sqlalchemy import Column, Table, Uuid, insert, select, update
from sqlalchemy.exc import SQLAlchemyError

from app.components.c11_kpi_analytics.store import delivery_facts, delivery_provider
from app.components.c12_simulation_replay.contracts import (
    Checkpoint,
    RecordingRecord,
    ReplayFrame,
    RunManifest,
    TimeRange,
)
from app.components.c12_simulation_replay.persistence import (
    ReplayRepository,
    encode,
    json_type,
    metadata,
)
from app.components.c12_simulation_replay.replay import (
    RecordedStep,
    SavedRun,
    snapshot_sha256,
)
from app.shared.contracts import EventEnvelope

captures = Table(
    "captures",
    metadata,
    Column("run_id", Uuid, primary_key=True),
    Column("provider_id", Uuid, nullable=False),
    Column("body", json_type, nullable=False),
)


class CaptureRecorder:
    """Trusted operator registers a run BEFORE its feed starts; no live capture.

    Only the original C04 outcomes are captured. No complete C05/C08/C10 history
    or simulation generator is implied. Sealed runs remain labelled incomplete.
    """

    def __init__(self, engine):
        self.engine = engine

    def begin(self, run_id, provider_id, start, configuration, synthetic):
        if start.tzinfo is None or start.utcoffset().total_seconds() != 0:
            raise ValueError("capture start must be UTC")
        if not isinstance(configuration, dict) or not isinstance(
            configuration.get("initialState"), dict
        ):
            raise ValueError(
                "supply an explicit recorded initialState in configuration"
            )
        body = {
            "start": start.isoformat(),
            "configuration": configuration,
            "synthetic": synthetic,
            "sealed": False,
            "events": [],
        }
        with self.engine.begin() as conn:
            conn.execute(
                insert(captures).values(
                    run_id=run_id, provider_id=provider_id, body=body
                )
            )

    def consume(self, event):
        if event.scope != "simulation":
            return "reject"
        try:
            with self.engine.begin() as conn:
                row = (
                    conn.execute(
                        select(captures)
                        .where(captures.c.run_id == event.run_id)
                        .with_for_update()
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    return "ack"  # capture is opt-in; never infer run ownership
                body = dict(row["body"])

                def reject(reason):
                    body["invalid"] = reason
                    conn.execute(
                        update(captures)
                        .where(captures.c.run_id == event.run_id)
                        .values(body=body)
                    )
                    return "reject"

                try:
                    provider_id = delivery_provider(event)
                except (KeyError, ValueError, TypeError):
                    return reject("Unusable C04 outcome")
                if provider_id is None:
                    return reject("Missing authoritative C04 providerId")
                if provider_id != row["provider_id"]:
                    # A shared simulation can contain other providers' deliveries.
                    # Never record their state into this provider's saved archive.
                    return "ack"
                # Foreign outcomes must not poison this provider's capture,
                # including when their delivery details are malformed.
                try:
                    delivery_facts(event)
                except (KeyError, ValueError, TypeError):
                    return reject("Unusable C04 outcome")
                value = event.model_dump(mode="json")
                for old in body["events"]:
                    if old["event_id"] == str(event.event_id):
                        return (
                            "ack"
                            if old == value
                            else reject("Conflicting duplicate event")
                        )
                if body["sealed"]:
                    return "reject"
                if len(body["events"]) >= 10000:
                    return reject("Capture size exceeded")
                # Observed time is the recorded timeline. Reject backward input
                # without inventing/reordering history; callers must repair it.
                previous = (
                    body["events"][-1]["observed_at"]
                    if body["events"]
                    else body["start"]
                )
                if event.observed_at < datetime.fromisoformat(previous):
                    return reject("Out-of-order observed time")
                body["events"] = [*body["events"], value]
                conn.execute(
                    update(captures)
                    .where(captures.c.run_id == event.run_id)
                    .values(body=body)
                )
            return "ack"
        except SQLAlchemyError:
            return "retry"

    def seal(self, run_id, end):
        """Freeze a bounded C04-only archive; never relabel it a complete run."""
        with self.engine.begin() as conn:
            row = (
                conn.execute(
                    select(captures)
                    .where(captures.c.run_id == run_id)
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            body = dict(row["body"])
            if body["sealed"]:
                raise ValueError("capture is already sealed")
            if body.get("invalid"):
                raise ValueError("capture contains an invalid or missing outcome")
            start = datetime.fromisoformat(body["start"])
            period = TimeRange(start=start, end=end)
            state = {
                **body["configuration"]["initialState"],
                "recordingScope": "C04 delivery outcomes only",
                "synthetic": body["synthetic"],
                "deliveries": body["configuration"]["initialState"].get(
                    "deliveries", {}
                ),
            }
            initial = ReplayFrame(
                session_id=uuid4(),
                run_id=run_id,
                cursor=0,
                status="ready",
                simulation_time=start,
                state=state,
                decision_ids=[],
                recorded_kpis=[],
            )
            checksum = snapshot_sha256(initial)
            checkpoint = Checkpoint(
                run_id=run_id,
                cursor=0,
                simulation_time=start,
                state_ref="c12://initial-state",
                state_sha256=checksum,
            )
            steps = []
            for sequence, raw in enumerate(body["events"], 1):
                event = EventEnvelope.model_validate(raw)
                if event.observed_at >= end:
                    raise ValueError("end must follow the last recorded outcome")
                state = json.loads(json.dumps(state))
                state["deliveries"][event.payload["deliveryId"]] = event.payload[
                    "delivery"
                ]
                frame = ReplayFrame(
                    session_id=initial.session_id,
                    run_id=run_id,
                    cursor=sequence,
                    status="playing",
                    simulation_time=event.observed_at,
                    state=state,
                    decision_ids=[],
                    recorded_kpis=[],
                )
                steps.append(
                    RecordedStep(
                        RecordingRecord(
                            run_id=run_id,
                            sequence=sequence,
                            simulation_time=event.observed_at,
                            event=event,
                        ),
                        frame,
                    )
                )
            manifest = RunManifest(
                run_id=run_id,
                provider_id=row["provider_id"],
                scenario_id="partial-c04-capture",
                seed=0,
                configuration_version="capture-v1",
                status="incomplete",
                initial_state_ref=checkpoint.state_ref,
                initial_state_sha256=checksum,
                available_ranges=[period],
                last_sequence=len(steps),
            )
            archive = SavedRun(manifest, checkpoint, initial, tuple(steps))
            # Import and seal atomically on the same database connection.
            repo = ReplayRepository(self.engine)
            data = encode(archive)
            config = {
                "version": "capture-v1",
                "parameters": body["configuration"],
                "synthetic": body["synthetic"],
                "scope": "C04 outcomes only",
            }
            data["configuration"] = config
            data["configurationSha256"] = hashlib.sha256(
                json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            repo.import_archive(data, connection=conn)
            body["sealed"] = True
            conn.execute(
                update(captures).where(captures.c.run_id == run_id).values(body=body)
            )
