"""Immutable saved recordings and transactional, persistent replay sessions.

Only a trusted operator imports a recording. HTTP playback has no import/live
writer/broker dependency. A saved recording is a complete bounded archive for
its declared scope, not evidence of a complete simulation-engine integration.
"""

import hashlib
import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    MetaData,
    String,
    Table,
    Uuid,
    delete,
    insert,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.components.c12_simulation_replay.contracts import (
    Checkpoint,
    RecordingRecord,
    ReplayFrame,
    ReplayRequest,
    RunManifest,
)
from app.components.c12_simulation_replay.replay import (
    RecordedStep,
    ReplayService,
    SavedRun,
    _Session,
)
from app.shared.result import Failure, Ok

metadata = MetaData(schema="c12_simulation_replay")
json_type = JSON().with_variant(JSONB, "postgresql")
runs = Table(
    "runs",
    metadata,
    Column("run_id", Uuid, primary_key=True),
    Column("provider_id", Uuid, nullable=False),
    Column("archive", json_type, nullable=False),
)
sessions = Table(
    "sessions",
    metadata,
    Column("session_id", Uuid, primary_key=True),
    Column("owner_id", Uuid, nullable=False),
    Column("run_id", Uuid, nullable=False),
    Column("body", json_type, nullable=False),
)
receipts = Table(
    "command_receipts",
    metadata,
    Column("provider_id", Uuid, primary_key=True),
    Column("owner_id", Uuid, primary_key=True),
    Column("operation", String(64), primary_key=True),
    Column("key", String(200), primary_key=True),
    Column("command", json_type, nullable=False),
    Column("result", json_type, nullable=False),
)

page_cursors = Table(
    "run_page_cursors",
    metadata,
    Column("token", Uuid, primary_key=True),
    Column("binding", String(64), nullable=False),
    Column("run_ids", json_type, nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
)


def encode(archive):
    return {
        "manifest": archive.manifest.model_dump(mode="json"),
        "checkpoint": archive.checkpoint.model_dump(mode="json"),
        "initial": archive.initial.model_dump(mode="json"),
        "steps": [
            {
                "record": s.record.model_dump(mode="json"),
                "frame": s.frame.model_dump(mode="json"),
            }
            for s in archive.steps
        ],
    }


def decode(data):
    return SavedRun(
        RunManifest.model_validate(data["manifest"]),
        Checkpoint.model_validate(data["checkpoint"]),
        ReplayFrame.model_validate(data["initial"]),
        tuple(
            RecordedStep(
                RecordingRecord.model_validate(s["record"]),
                ReplayFrame.model_validate(s["frame"]),
            )
            for s in data["steps"]
        ),
    )


class FrozenReader:
    def __init__(self, archive):
        self.archive = archive

    def load(self, run_id):
        return self.archive if self.archive.manifest.run_id == run_id else None

    def read_step(self, run_id, sequence):
        if run_id != self.archive.manifest.run_id:
            raise ValueError("wrong run")
        return self.archive.steps[sequence - self.archive.checkpoint.cursor - 1]


class ReplayRepository:
    def __init__(self, engine):
        self.engine = engine
        self.replayed = False

    def import_archive(self, data, connection=None):
        """Trusted CLI, not an HTTP operation. Never overwrite saved history."""
        archive = decode(data)
        config = data.get("configuration")
        if (
            not isinstance(config, dict)
            or config.get("version") != archive.manifest.configuration_version
        ):
            raise ValueError("recording requires its immutable versioned configuration")
        checksum = hashlib.sha256(
            json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if data.get("configurationSha256") != checksum:
            raise ValueError("configuration integrity mismatch")
        # Verify every available range, bounded to keep a small-run prototype safe.
        if not archive.manifest.available_ranges or len(archive.steps) > 10000:
            raise ValueError("recording needs ranges and at most 10000 outcomes")
        for period in archive.manifest.available_ranges:
            result = ReplayService(FrozenReader(archive), lambda *_: True).start_replay(
                UUID(int=0),
                ReplayRequest(run_id=archive.manifest.run_id, time_range=period),
            )
            if isinstance(result, Failure) or not isinstance(result.value, ReplayFrame):
                raise ValueError("recording is not reconstructable")
        with (
            nullcontext(connection) if connection is not None else self.engine.begin()
        ) as conn:
            conn.execute(
                insert(runs).values(
                    run_id=archive.manifest.run_id,
                    provider_id=archive.manifest.provider_id,
                    archive=dict(
                        encode(archive),
                        configuration=config,
                        configurationSha256=checksum,
                    ),
                )
            )

    def list_runs(self, context, filters=None, limit=20, cursor=None):
        filters = filters or {}
        binding = hashlib.sha256(
            json.dumps(
                {
                    "principal": str(context.principal_id),
                    "provider": str(context.acting_provider_id),
                    "filters": filters,
                    "limit": limit,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        now = datetime.now(UTC)
        with self.engine.begin() as conn:
            if cursor:
                try:
                    token = UUID(cursor)
                except ValueError:
                    return Failure("Invalid", "Invalid cursor")
                saved = (
                    conn.execute(
                        select(page_cursors).where(
                            page_cursors.c.token == token,
                            page_cursors.c.binding == binding,
                            page_cursors.c.expires_at > now,
                        )
                    )
                    .mappings()
                    .first()
                )
                if not saved:
                    return Failure("Invalid", "Cursor expired or filters changed")
                ids = saved["run_ids"]
            else:
                values = conn.execute(
                    select(runs.c.archive)
                    .where(runs.c.provider_id == context.acting_provider_id)
                    .order_by(runs.c.run_id)
                ).scalars()
                ids = []
                for value in values:
                    manifest = RunManifest.model_validate(value["manifest"])
                    if (
                        filters.get("scenarioId")
                        and manifest.scenario_id != filters["scenarioId"]
                    ):
                        continue
                    if filters.get("status") and manifest.status != filters["status"]:
                        continue
                    if filters.get("start") and not any(
                        r.start < datetime.fromisoformat(filters["end"])
                        and datetime.fromisoformat(filters["start"]) < r.end
                        for r in manifest.available_ranges
                    ):
                        continue
                    ids.append(str(manifest.run_id))
            selected = ids[:limit]
            values = conn.execute(
                select(runs.c.archive).where(
                    runs.c.provider_id == context.acting_provider_id,
                    runs.c.run_id.in_([UUID(v) for v in selected]),
                )
            ).scalars()
            by_id = {v["manifest"]["run_id"]: v["manifest"] for v in values}
            next_cursor = None
            if len(ids) > limit:
                next_cursor = str(uuid4())
                conn.execute(
                    delete(page_cursors).where(page_cursors.c.expires_at <= now)
                )
                conn.execute(
                    insert(page_cursors).values(
                        token=UUID(next_cursor),
                        binding=binding,
                        run_ids=ids[limit:],
                        expires_at=now + timedelta(hours=1),
                    )
                )
            return Ok(
                {
                    "items": [
                        RunManifest.model_validate(by_id[v]).model_dump(
                            mode="json", by_alias=True
                        )
                        for v in selected
                    ],
                    "nextCursor": next_cursor,
                }
            )

    def _archive(self, conn, context, run_id):
        raw = conn.execute(
            select(runs.c.archive).where(
                runs.c.run_id == run_id,
                runs.c.provider_id == context.acting_provider_id,
            )
        ).scalar_one_or_none()
        return decode(raw) if raw is not None else None

    def start(self, context, request, key):
        command = request.model_dump(mode="json")
        with self.engine.begin() as conn:
            archive = self._archive(conn, context, request.run_id)
            if archive is None:
                return Failure("NotFound", "Saved run not found in this provider")
            receipt = (
                conn.execute(
                    select(receipts).where(
                        receipts.c.provider_id == context.acting_provider_id,
                        receipts.c.owner_id == context.principal_id,
                        receipts.c.key == key,
                        receipts.c.operation == "start",
                    )
                )
                .mappings()
                .first()
            )
            if receipt:
                if receipt["operation"] != "start" or receipt["command"] != command:
                    return Failure(
                        "Conflict", "Idempotency key reused with changed input"
                    )
                self.replayed = True
                return Ok(ReplayFrame.model_validate(receipt["result"]))
            service = ReplayService(FrozenReader(archive), lambda *_: True)
            result = service.start_replay(context.principal_id, request)
            if isinstance(result, Failure) or not isinstance(result.value, ReplayFrame):
                return result
            frame = result.value
            session = service._sessions[frame.session_id]
            conn.execute(
                insert(sessions).values(
                    session_id=frame.session_id,
                    owner_id=context.principal_id,
                    run_id=request.run_id,
                    body={
                        "request": command,
                        "frame": frame.model_dump(mode="json"),
                        "last_cursor": session.last_cursor,
                    },
                )
            )
            conn.execute(
                insert(receipts).values(
                    provider_id=context.acting_provider_id,
                    owner_id=context.principal_id,
                    operation="start",
                    key=key,
                    command=command,
                    result=frame.model_dump(mode="json"),
                )
            )
            return result

    def operate(self, context, session_id, command=None, key=None, tick=None):
        """Row lock serialises controls/ticks across API processes, not just threads."""
        with self.engine.begin() as conn:
            row = (
                conn.execute(
                    select(sessions)
                    .where(
                        sessions.c.session_id == session_id,
                        sessions.c.owner_id == context.principal_id,
                    )
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if row is None:
                return Failure("NotFound", "Replay session not found")
            archive = self._archive(conn, context, row["run_id"])
            if archive is None:
                return Failure("NotFound", "Saved run not found in this provider")
            body = row["body"]
            frame = ReplayFrame.model_validate(body["frame"])
            if command is None and tick is None:
                return Ok(frame)  # polling never advances
            if command is not None:
                saved = (
                    conn.execute(
                        select(receipts).where(
                            receipts.c.provider_id == context.acting_provider_id,
                            receipts.c.owner_id == context.principal_id,
                            receipts.c.key == key,
                            receipts.c.operation == f"control:{session_id}",
                        )
                    )
                    .mappings()
                    .first()
                )
                wire = command.model_dump(mode="json")
                if saved:
                    if (
                        saved["operation"] != f"control:{session_id}"
                        or saved["command"] != wire
                    ):
                        return Failure(
                            "Conflict", "Idempotency key reused with changed input"
                        )
                    return Ok(ReplayFrame.model_validate(saved["result"]))
            service = ReplayService(FrozenReader(archive), lambda *_: True)
            service._sessions[session_id] = _Session(
                context.principal_id,
                ReplayRequest.model_validate(body["request"]),
                archive,
                frame,
                body["last_cursor"],
            )
            result = (
                service.control_replay(context.principal_id, command)
                if command is not None
                else service.advance_replay(context.principal_id, session_id, tick)
            )
            if isinstance(result, Failure):
                return result
            body = dict(body, frame=result.value.model_dump(mode="json"))
            conn.execute(
                update(sessions)
                .where(sessions.c.session_id == session_id)
                .values(body=body)
            )
            if command is not None:
                conn.execute(
                    insert(receipts).values(
                        provider_id=context.acting_provider_id,
                        owner_id=context.principal_id,
                        operation=f"control:{session_id}",
                        key=key,
                        command=wire,
                        result=result.value.model_dump(mode="json"),
                    )
                )
            return result
