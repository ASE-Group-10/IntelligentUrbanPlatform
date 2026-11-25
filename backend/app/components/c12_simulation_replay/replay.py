"""Deterministic replay of recorded snapshots; no operational write dependencies."""

import hashlib
import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from threading import RLock
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import ValidationError

from app.components.c12_simulation_replay.contracts import (
    CannotReplay,
    Checkpoint,
    RecordingRecord,
    ReplayControl,
    ReplayFrame,
    ReplayRequest,
    RunManifest,
)
from app.shared.result import Failure, Ok, Result


@dataclass(frozen=True)
class RecordedStep:
    """Private archive adapter value: full saved state AFTER this source outcome.

    This is not a new producer event schema. The recorder must capture complete
    snapshots including original KPI/decision values. Replay never recalculates.
    """

    record: RecordingRecord
    frame: ReplayFrame


@dataclass(frozen=True)
class SavedRun:
    manifest: RunManifest
    checkpoint: Checkpoint
    initial: ReplayFrame
    steps: tuple[RecordedStep, ...]


class RecordingUnavailableError(Exception):
    """Read failure; never include credentials or raw database errors."""


class ArchiveReader(Protocol):
    """Read-only boundary, with no publish, command or live-state write method."""

    def load(self, run_id: UUID) -> SavedRun | None: ...

    def read_step(self, run_id: UUID, sequence: int) -> RecordedStep: ...


@dataclass
class _Session:
    owner: UUID
    request: ReplayRequest
    archive: SavedRun
    frame: ReplayFrame
    last_cursor: int


def snapshot_sha256(frame: ReplayFrame) -> str:
    """Digest canonical recorded content, excluding presentation/session fields."""
    value = frame.model_dump(mode="json", by_alias=True)
    content = {k: value[k] for k in ("state", "decisionIds", "recordedKpis")}
    return hashlib.sha256(
        json.dumps(
            content, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


class ReplayService:
    """Process-local timeline engine for #17.

    The trusted host supplies an authenticated principal ID and a C02 access
    policy. There is no default policy. This is not an HTTP authentication API.
    Archive and session copies prevent caller mutations changing saved history.
    Real database/broker ACL isolation must be verified separately.
    """

    def __init__(
        self,
        reader: ArchiveReader,
        authorise: Callable[[UUID, UUID], bool],
        new_id: Callable[[], UUID] = uuid4,
        max_sessions: int = 100,
    ) -> None:
        if type(max_sessions) is not int or max_sessions < 1:
            raise ValueError("session capacity must be a positive integer")
        self._reader = reader
        self._authorise = authorise
        self._new_id = new_id
        self._capacity = max_sessions
        self._sessions: dict[UUID, _Session] = {}
        self._lock = RLock()

    def start_replay(
        self, principal_id: UUID, request: ReplayRequest
    ) -> Result[ReplayFrame | CannotReplay]:
        """Load and verify a reconstructable range, returning its initial frame."""
        request = ReplayRequest.model_validate(request.model_dump())
        if not self._authorise(principal_id, request.run_id):
            return Failure("Forbidden", "C12 run is not authorised")
        with self._lock:
            try:
                archive = deepcopy(self._reader.load(request.run_id))
            except RecordingUnavailableError:
                return Failure("Unavailable", "C12 archive unavailable")
            if archive is None:
                return Failure("NotFound", "C12 run not found")
            try:
                _validate(archive, request)
            except (ValueError, TypeError, AttributeError):
                return Ok(
                    CannotReplay(
                        run_id=request.run_id,
                        reason="Recorded range or checkpoint is not reconstructable",
                        available_ranges=[],
                    )
                )
            if len(self._sessions) >= self._capacity:
                return Failure("Unavailable", "C12 session capacity reached")
            session_id = self._new_id()
            if session_id in self._sessions:
                return Failure("Conflict", "C12 duplicate session identity")
            # Apply all saved outcomes before start, preserving order at equal times.
            initial = archive.initial
            last = archive.checkpoint.cursor
            for step in archive.steps:
                if step.record.simulation_time < request.time_range.start:
                    initial = step.frame
                if step.record.simulation_time < request.time_range.end:
                    last = step.record.sequence
            frame = initial.model_copy(
                deep=True,
                update={
                    "session_id": session_id,
                    "status": "ready",
                    "interruption": None,
                },
            )
            self._sessions[session_id] = _Session(
                principal_id, request, archive, frame, last
            )
            return Ok(frame.model_copy(deep=True))

    def get_frame(self, principal_id: UUID, session_id: UUID) -> Result[ReplayFrame]:
        """Read-only polling; every call rechecks owner and run authorisation."""
        with self._lock:
            session = self._get(principal_id, session_id)
            if isinstance(session, Failure):
                return session
            return Ok(session.frame.model_copy(deep=True))

    def control_replay(
        self, principal_id: UUID, control: ReplayControl
    ) -> Result[ReplayFrame]:
        """Apply a legal transition with optimistic cursor checking."""
        control = ReplayControl.model_validate(control.model_dump())
        with self._lock:
            session = self._get(principal_id, control.session_id)
            if isinstance(session, Failure):
                return session
            frame = session.frame
            if control.expected_cursor != frame.cursor:
                return Failure("Conflict", "C12 cursor changed")
            allowed = {
                "play": ("ready",),
                "pause": ("playing",),
                "resume": ("paused",),
                "seek": ("ready", "playing", "paused"),
                "stop": ("ready", "playing", "paused"),
            }
            if frame.status not in allowed[control.command]:
                return Failure("Conflict", "C12 incompatible timeline transition")
            if control.command == "seek":
                target = control.target_cursor
                floor = self._initial_cursor(session)
                if target is None or not floor <= target <= session.last_cursor:
                    return Failure("Invalid", "C12 seek target outside selected range")
                candidate = session.archive.initial
                try:
                    for step in session.archive.steps:
                        if step.record.sequence > target:
                            break
                        candidate = self._read_verified(session, step.record.sequence)
                except RecordingUnavailableError:
                    return self._pause(session)
                session.frame = candidate.model_copy(
                    deep=True,
                    update={
                        "session_id": control.session_id,
                        "status": "paused",
                        "interruption": None,
                    },
                )
            else:
                status = {
                    "play": "playing",
                    "pause": "paused",
                    "resume": "playing",
                    "stop": "stopped",
                }[control.command]
                session.frame = frame.model_copy(
                    deep=True, update={"status": status, "interruption": None}
                )
            return Ok(session.frame.model_copy(deep=True))

    def advance_replay(
        self, principal_id: UUID, session_id: UUID, expected_cursor: int
    ) -> Result[ReplayFrame]:
        """Trusted scheduler tick: one full outcome, or completion, or pause."""
        if type(expected_cursor) is not int or expected_cursor < 0:
            return Failure("Invalid", "C12 expected cursor must be nonnegative")
        with self._lock:
            session = self._get(principal_id, session_id)
            if isinstance(session, Failure):
                return session
            frame = session.frame
            if frame.cursor != expected_cursor or frame.status != "playing":
                return Failure("Conflict", "C12 cursor or playback status changed")
            if frame.cursor == session.last_cursor:
                session.frame = frame.model_copy(update={"status": "completed"})
            else:
                try:
                    candidate = self._read_verified(session, frame.cursor + 1)
                except RecordingUnavailableError:
                    return self._pause(session)
                session.frame = candidate.model_copy(
                    deep=True,
                    update={
                        "session_id": session_id,
                        "status": "playing",
                        "interruption": None,
                    },
                )
            return Ok(session.frame.model_copy(deep=True))

    def release_session(self, principal_id: UUID, session_id: UUID) -> Result[None]:
        """Host cleanup of a terminal session; no public contract is added."""
        with self._lock:
            session = self._get(principal_id, session_id)
            if isinstance(session, Failure):
                return session
            if session.frame.status not in ("completed", "stopped"):
                return Failure("Conflict", "C12 active session cannot be released")
            del self._sessions[session_id]
            return Ok(None)

    def _get(self, principal_id: UUID, session_id: UUID) -> _Session | Failure:
        session = self._sessions.get(session_id)
        if session is None or session.owner != principal_id:
            return Failure("NotFound", "C12 session not found")
        if not self._authorise(principal_id, session.request.run_id):
            return Failure("Forbidden", "C12 run access revoked")
        return session

    def _read_verified(self, session: _Session, sequence: int) -> ReplayFrame:
        step = self._reader.read_step(session.request.run_id, sequence)
        saved = session.archive.steps[sequence - session.archive.checkpoint.cursor - 1]
        if step != saved:
            raise RecordingUnavailableError("C12 recording changed")
        return step.frame.model_copy(deep=True)

    @staticmethod
    def _initial_cursor(session: _Session) -> int:
        cursor = session.archive.checkpoint.cursor
        for step in session.archive.steps:
            if step.record.simulation_time < session.request.time_range.start:
                cursor = step.record.sequence
        return cursor

    @staticmethod
    def _pause(session: _Session) -> Ok[ReplayFrame]:
        session.frame = session.frame.model_copy(
            deep=True,
            update={"status": "paused", "interruption": "C12 recording unavailable"},
        )
        return Ok(session.frame.model_copy(deep=True))


def _validate(archive: SavedRun, request: ReplayRequest) -> None:
    """Fail closed on incomplete sequences or invalid captured state."""
    manifest = RunManifest.model_validate(archive.manifest.model_dump())
    checkpoint = Checkpoint.model_validate(archive.checkpoint.model_dump())
    initial = ReplayFrame.model_validate(archive.initial.model_dump())
    selected = request.time_range
    if (
        manifest.run_id != request.run_id
        or checkpoint.run_id != request.run_id
        or initial.run_id != request.run_id
        or initial.cursor != checkpoint.cursor
        or initial.simulation_time != checkpoint.simulation_time
        or checkpoint.simulation_time > selected.start
        or snapshot_sha256(initial) != checkpoint.state_sha256
        or not any(
            r.start <= selected.start and selected.end <= r.end
            for r in manifest.available_ranges
        )
    ):
        raise ValueError("invalid checkpoint or range")
    if checkpoint.cursor == 0 and (
        checkpoint.state_ref != manifest.initial_state_ref
        or checkpoint.state_sha256 != manifest.initial_state_sha256
    ):
        raise ValueError("initial state does not match manifest")
    cursor, previous_time = checkpoint.cursor, checkpoint.simulation_time
    event_ids = set()
    for step in archive.steps:
        record = RecordingRecord.model_validate(step.record.model_dump())
        try:
            frame = ReplayFrame.model_validate(step.frame.model_dump())
        except ValidationError as error:
            raise ValueError("invalid recorded frame") from error
        cursor += 1
        if (
            record.run_id != request.run_id
            or record.sequence != cursor
            or record.event.event_id in event_ids
            or record.simulation_time < previous_time
            or frame.run_id != request.run_id
            or frame.cursor != cursor
            or frame.simulation_time != record.simulation_time
        ):
            raise ValueError("missing, duplicated or inconsistent outcome")
        # Reject state that cannot be represented as strict, canonical JSON.
        snapshot_sha256(frame)
        event_ids.add(record.event.event_id)
        previous_time = record.simulation_time
    if cursor != manifest.last_sequence:
        raise ValueError("essential recording missing")
