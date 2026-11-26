"""Timeline behaviour using synthetic saved outcomes, without real ACL claims."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest

from app.components.c12_simulation_replay.contracts import (
    CannotReplay,
    Checkpoint,
    RecordingRecord,
    ReplayControl,
    ReplayFrame,
    ReplayRequest,
    RunManifest,
)
from app.components.c12_simulation_replay.replay import (
    RecordedStep,
    RecordingUnavailableError,
    ReplayService,
    SavedRun,
    snapshot_sha256,
)
from app.shared.result import Ok


class SyntheticReader:
    def __init__(self, archive):
        self.archive = archive
        self.fail_at = None
        self.calls = []

    def load(self, run_id):
        self.calls.append(("load", run_id))
        return self.archive

    def read_step(self, run_id, sequence):
        self.calls.append(("read", sequence))
        if sequence == self.fail_at:
            raise RecordingUnavailableError("synthetic interruption")
        return self.archive.steps[sequence - 1]


@pytest.fixture
def setup(example):
    request = ReplayRequest.model_validate(example("start-replay-request.json"))
    manifest = RunManifest.model_validate(example("run-manifest.json"))
    checkpoint = Checkpoint.model_validate(example("checkpoint.json"))
    first = ReplayFrame.model_validate(example("replay-frame-paused.json"))
    first.interruption = None
    first.status = "playing"
    initial = first.model_copy(
        deep=True,
        update={
            "cursor": 0,
            "status": "ready",
            "simulation_time": checkpoint.simulation_time,
            "state": {"synthetic": True, "completedDeliveries": 0},
            "recorded_kpis": [],
        },
    )
    checkpoint.state_sha256 = snapshot_sha256(initial)
    manifest.initial_state_sha256 = checkpoint.state_sha256
    manifest.last_sequence = 2
    record = RecordingRecord.model_validate(example("recording-record.json"))
    second_record = record.model_copy(deep=True)
    second_record.sequence = 2
    second_record.event.event_id = uuid4()
    # Equal timestamps must still apply two outcomes in sequence order.
    second = first.model_copy(deep=True)
    second.cursor = 2
    second.state["completedDeliveries"] = 2
    archive = SavedRun(
        manifest,
        checkpoint,
        initial,
        (RecordedStep(record, first), RecordedStep(second_record, second)),
    )
    reader = SyntheticReader(archive)
    owner = uuid4()
    allowed = {owner}
    service = ReplayService(reader, lambda principal, run: principal in allowed)
    return service, reader, request, owner, allowed


def command(service, owner, frame, action, target=None):
    return service.control_replay(
        owner,
        ReplayControl(
            session_id=frame.session_id,
            command=action,
            expected_cursor=frame.cursor,
            target_cursor=target,
        ),
    )


def test_full_timeline_order_recorded_values_and_completion(setup):
    service, reader, request, owner, _ = setup
    frame = service.start_replay(owner, request).value
    assert frame.status == "ready" and frame.cursor == 0
    frame = command(service, owner, frame, "play").value
    first = service.advance_replay(owner, frame.session_id, 0).value
    assert first.cursor == 1 and first.state["completedDeliveries"] == 1
    assert first.recorded_kpis == reader.archive.steps[0].frame.recorded_kpis
    second = service.advance_replay(owner, frame.session_id, 1).value
    assert second.cursor == 2 and second.state["completedDeliveries"] == 2
    done = service.advance_replay(owner, frame.session_id, 2).value
    assert done.status == "completed"
    assert command(service, owner, done, "resume").kind == "Conflict"
    assert isinstance(service.release_session(owner, done.session_id), Ok)


def test_polling_does_not_advance_and_returns_defensive_copy(setup):
    service, reader, request, owner, _ = setup
    frame = service.start_replay(owner, request).value
    frame.state["completedDeliveries"] = 999
    before = len(reader.calls)
    polled = service.get_frame(owner, frame.session_id).value
    assert polled.cursor == 0 and polled.state["completedDeliveries"] == 0
    assert len(reader.calls) == before


def test_pause_resume_seek_and_stop(setup):
    service, _, request, owner, _ = setup
    frame = service.start_replay(owner, request).value
    assert command(service, owner, frame, "pause").kind == "Conflict"
    frame = command(service, owner, frame, "play").value
    frame = service.advance_replay(owner, frame.session_id, 0).value
    frame = command(service, owner, frame, "pause").value
    assert service.advance_replay(owner, frame.session_id, 1).kind == "Conflict"
    frame = command(service, owner, frame, "resume").value
    frame = command(service, owner, frame, "seek", 0).value
    assert frame.cursor == 0 and frame.status == "paused"
    frame = command(service, owner, frame, "seek", 2).value
    assert frame.cursor == 2 and frame.state["completedDeliveries"] == 2
    assert command(service, owner, frame, "seek", 3).kind == "Invalid"
    frame = command(service, owner, frame, "stop").value
    assert frame.status == "stopped"
    assert command(service, owner, frame, "play").kind == "Conflict"


def test_interruption_and_resume_retry_same_next_outcome(setup):
    service, reader, request, owner, _ = setup
    frame = service.start_replay(owner, request).value
    frame = command(service, owner, frame, "play").value
    frame = service.advance_replay(owner, frame.session_id, 0).value
    reader.fail_at = 2
    paused = service.advance_replay(owner, frame.session_id, 1).value
    assert paused.status == "paused" and paused.cursor == 1
    assert paused.state == frame.state and paused.interruption
    reader.fail_at = None
    frame = command(service, owner, paused, "resume").value
    assert frame.interruption is None
    frame = service.advance_replay(owner, frame.session_id, 1).value
    assert frame.cursor == 2
    assert reader.calls[-2:] == [("read", 2), ("read", 2)]


def test_failed_seek_keeps_previous_successful_frame(setup):
    service, reader, request, owner, _ = setup
    frame = service.start_replay(owner, request).value
    reader.fail_at = 2
    paused = command(service, owner, frame, "seek", 2).value
    assert paused.cursor == 0 and paused.state == frame.state
    assert paused.status == "paused" and paused.interruption


def test_owner_revocation_and_stale_cursor_are_checked(setup):
    service, reader, request, owner, allowed = setup
    outsider = uuid4()
    assert service.start_replay(outsider, request).kind == "Forbidden"
    assert not reader.calls
    frame = service.start_replay(owner, request).value
    assert service.get_frame(outsider, frame.session_id).kind == "NotFound"
    frame = command(service, owner, frame, "play").value
    service.advance_replay(owner, frame.session_id, 0)
    assert service.advance_replay(owner, frame.session_id, 0).kind == "Conflict"
    assert command(service, owner, frame, "pause").kind == "Conflict"
    allowed.clear()
    before = len(reader.calls)
    assert service.get_frame(owner, frame.session_id).kind == "Forbidden"
    assert service.advance_replay(owner, frame.session_id, 1).kind == "Forbidden"
    assert len(reader.calls) == before


@pytest.mark.parametrize(
    "corruption",
    ["gap", "duplicate", "hash", "run", "time", "frame", "tail", "range"],
)
def test_invalid_or_incomplete_history_cannot_start(setup, corruption):
    service, reader, request, owner, _ = setup
    archive = deepcopy(reader.archive)
    if corruption == "gap":
        archive.steps[0].record.sequence = 2
    elif corruption == "duplicate":
        archive.steps[1].record.event.event_id = archive.steps[0].record.event.event_id
    elif corruption == "hash":
        archive.initial.state["tampered"] = True
    elif corruption == "run":
        archive.steps[0].record.run_id = uuid4()
    elif corruption == "time":
        archive.steps[1].record.simulation_time -= timedelta(hours=1)
    elif corruption == "frame":
        archive.steps[0].frame.cursor = 22
    elif corruption == "tail":
        archive = replace(archive, steps=archive.steps[:1])
    else:
        request.time_range.end += timedelta(days=1)
    reader.archive = archive
    assert isinstance(service.start_replay(owner, request).value, CannotReplay)


def test_changed_recording_pauses_without_applying_mutation(setup):
    service, reader, request, owner, _ = setup
    frame = service.start_replay(owner, request).value
    frame = command(service, owner, frame, "play").value
    reader.archive.steps[0].frame.state["tampered"] = True
    paused = service.advance_replay(owner, frame.session_id, 0).value
    assert paused.cursor == 0 and "tampered" not in paused.state
    assert paused.status == "paused"


def test_half_open_range_and_reconstruction_before_start(setup):
    service, _, request, owner, _ = setup
    request.time_range.start += timedelta(minutes=11)
    frame = service.start_replay(owner, request).value
    assert frame.cursor == 2 and frame.state["completedDeliveries"] == 2
    assert command(service, owner, frame, "seek", 1).kind == "Invalid"
    request.time_range.start -= timedelta(minutes=11)
    request.time_range.end = request.time_range.start + timedelta(minutes=10)
    frame = service.start_replay(owner, request).value
    frame = command(service, owner, frame, "play").value
    done = service.advance_replay(owner, frame.session_id, 0).value
    assert done.status == "completed" and done.cursor == 0


def test_capacity_and_invalid_scheduler_cursor(setup):
    _, reader, request, owner, _ = setup
    service = ReplayService(reader, lambda *_: True, max_sessions=1)
    frame = service.start_replay(owner, request).value
    assert service.start_replay(owner, request).kind == "Unavailable"
    for cursor in (True, -1, 0.5):
        assert service.advance_replay(owner, frame.session_id, cursor).kind == "Invalid"
    assert service.release_session(owner, frame.session_id).kind == "Conflict"


def test_reader_has_no_live_writes_or_publisher(setup):
    service, reader, request, owner, _ = setup
    # Successful execution with a reader exposing only load/read_step proves the
    # service needs no write collaborator. It does not prove real broker/DB ACLs.
    frame = service.start_replay(owner, request).value
    frame = command(service, owner, frame, "play").value
    service.advance_replay(owner, frame.session_id, 0)
    assert set(name for name, _ in reader.calls) == {"load", "read"}
