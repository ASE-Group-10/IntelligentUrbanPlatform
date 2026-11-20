"""UC06 evaluateChange for a simulated road closure (thin slice).

C05 is replaced by ``FakePlanning`` and the database by the in-memory store, both
test doubles. The shared contract examples describe one closure of ``seg:4471023``
used by one plan, so expected values are read from them where possible.
"""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.components.c07_disruption_detection.contracts import (
    AffectedWork,
    ReplanOutcome,
    ReplanRequest,
)
from app.components.c07_disruption_detection.detection import DisruptionDetection
from app.components.c07_disruption_detection.store import (
    DisruptionStoreUnavailableError,
    InMemoryDisruptionStore,
    InMemoryDisruptionTransaction,
)
from app.shared.contracts import EventEnvelope
from app.shared.result import Failure, Ok
from tests.c07_planning_double import FakePlanning

EVENT_ID = UUID("5b1e0c1a-8f0e-4a7e-9d6c-1f2a3b4c5d6e")
CORRELATION_ID = UUID("a7d9e2f4-3c1b-4b8a-9e6f-0d1c2b3a4f5e")
RUN_ID = UUID("c3f1a2b4-5d6e-4f70-8a9b-0c1d2e3f4a5b")
DISRUPTION_ID = UUID("d4c3b2a1-0f9e-4d8c-b7a6-5e4d3c2b1a0f")
PLAN_ID = UUID("9e2c4b6a-1d3f-4e5a-8b7c-6d5e4f3a2b1c")
DECISION_ID = UUID("e5f6a7b8-c9d0-4e1f-a2b3-c4d5e6f7a8b9")
NOW = datetime(2026, 11, 20, 8, 14, 32, tzinfo=UTC)
SEGMENT = "seg:4471023"
SECOND_SEGMENT = "seg:4471024"
UNUSED_SEGMENT = "seg:999"


def make_service(planning: FakePlanning, store: InMemoryDisruptionStore):
    return DisruptionDetection(
        planning=planning,
        store=store,
        clock=lambda: NOW,
        new_id=lambda: DISRUPTION_ID,
    )


def make_data(example, *, segment=SEGMENT, status="closed", quality="good") -> dict:
    """The contract's CityStateUpdated envelope as JSON, with its change adjusted."""
    data = example("city-state-updated-event.json")
    data["payload"]["changes"][0] |= {
        "segmentKey": segment,
        "status": status,
        "quality": quality,
    }
    data["affected"]["segmentKeys"] = [segment]
    return data


def make_event(example, *, overrides=None, **change) -> EventEnvelope:
    """Envelope with one change adjusted; ``overrides`` replace envelope fields."""
    return EventEnvelope.model_validate(
        make_data(example, **change) | (overrides or {})
    )


@pytest.fixture
def affected_work(example) -> AffectedWork:
    return AffectedWork.model_validate(example("find-affected-work-response.json"))


@pytest.fixture
def planning(affected_work) -> FakePlanning:
    """C05 double in which one plan uses ``SEGMENT`` and no plan uses any other."""
    return FakePlanning(
        work_by_segment={SEGMENT: affected_work},
        outcome=ReplanOutcome(
            status="PlanSaved", new_plan_version=4, decision_id=DECISION_ID
        ),
    )


@pytest.fixture
def store() -> InMemoryDisruptionStore:
    return InMemoryDisruptionStore()


def test_closure_on_used_segment_creates_high_disruption_and_one_replan(
    example, planning, store
):
    result = make_service(planning, store).evaluate_change(make_event(example))

    assert isinstance(result, Ok)
    assert result.value.kind == "created"
    assert (result.value.disruption_id, result.value.disruption_version) == (
        DISRUPTION_ID,
        1,
    )
    (disruption,) = store.disruptions("simulation", RUN_ID)
    assert (disruption.version, disruption.severity, disruption.status) == (
        1,
        "high",
        "active",
    )
    assert disruption.cause == "roadworks"
    assert disruption.segment_keys == [SEGMENT]
    assert disruption.trigger_event_ids == [EVENT_ID]
    assert len(planning.replan_calls) == 1
    (request, correlation_id) = planning.replan_calls[0]
    assert request.key == f"{PLAN_ID}:{DISRUPTION_ID}:1"
    assert request == ReplanRequest.model_validate(example("request-replan.json"))
    assert correlation_id == CORRELATION_ID
    assert result.value.replan_requests == [request]


def test_replan_outcome_is_recorded_without_bumping_the_version(
    example, planning, store
):
    make_service(planning, store).evaluate_change(make_event(example))

    (disruption,) = store.disruptions("simulation", RUN_ID)
    assert disruption.version == 1
    (plan,) = disruption.affected_plans
    assert (plan.plan_id, plan.plan_version) == (PLAN_ID, 3)
    assert (plan.replan_requested, plan.replan_outcome) == (True, "PlanSaved")


def test_same_event_twice_is_a_duplicate_without_new_effects(example, planning, store):
    service = make_service(planning, store)
    service.evaluate_change(make_event(example))

    second = service.evaluate_change(make_event(example))

    assert isinstance(second, Ok)
    assert second.value.kind == "duplicate"
    assert len(planning.replan_calls) == 1
    assert len(store.disruptions("simulation", RUN_ID)) == 1


def test_closure_on_unused_segment_is_no_disruption_but_event_is_processed(
    example, planning, store
):
    event = make_event(example, segment=UNUSED_SEGMENT)

    result = make_service(planning, store).evaluate_change(event)

    assert isinstance(result, Ok)
    assert result.value.kind == "no_disruption"
    assert result.value.reason
    assert store.disruptions("simulation", RUN_ID) == []
    assert planning.replan_calls == []
    assert store.processed("simulation", RUN_ID) == {EVENT_ID}


def test_c05_unavailable_is_unknown_and_event_is_not_processed(
    example, planning, store
):
    planning.unavailable = True

    result = make_service(planning, store).evaluate_change(make_event(example))

    assert isinstance(result, Ok)
    assert result.value.kind == "unknown"
    assert result.value.retry_later is True
    assert store.disruptions("simulation", RUN_ID) == []
    assert planning.replan_calls == []
    assert store.processed("simulation", RUN_ID) == set()


def test_closure_with_degraded_quality_is_unknown_without_calling_c05(
    example, planning, store
):
    event = make_event(example, quality="degraded")

    result = make_service(planning, store).evaluate_change(event)

    assert isinstance(result, Ok)
    assert result.value.kind == "unknown"
    assert planning.find_calls == []
    assert store.processed("simulation", RUN_ID) == set()


def test_slow_segment_is_no_disruption_without_calling_c05(example, planning, store):
    event = make_event(example, status="slow")

    result = make_service(planning, store).evaluate_change(event)

    assert isinstance(result, Ok)
    assert result.value.kind == "no_disruption"
    assert planning.find_calls == []
    assert store.disruptions("simulation", RUN_ID) == []


def test_unavailable_store_fails_and_no_replan_is_requested(example, planning):
    class SaveFails(InMemoryDisruptionTransaction):
        def save_disruption(self, disruption) -> None:
            raise DisruptionStoreUnavailableError("disruption write failed")

    store = InMemoryDisruptionStore(transaction_type=SaveFails)

    result = make_service(planning, store).evaluate_change(make_event(example))

    assert isinstance(result, Failure)
    assert result.kind == "Unavailable"
    assert planning.replan_calls == []
    assert store.disruptions("simulation", RUN_ID) == []
    assert store.processed("simulation", RUN_ID) == set()


def test_same_event_id_in_another_scope_is_not_a_duplicate(example, planning, store):
    service = make_service(planning, store)
    service.evaluate_change(make_event(example))
    live_event = make_event(example, overrides={"scope": "live", "runId": None})

    result = service.evaluate_change(live_event)

    assert isinstance(result, Ok)
    assert result.value.kind == "created"
    assert len(store.disruptions("live", None)) == 1
    assert len(store.disruptions("simulation", RUN_ID)) == 1
    assert len(planning.replan_calls) == 2


def test_event_type_not_handled_yet_is_unknown(example, planning, store):
    event = make_event(example, overrides={"eventType": "ResourceAvailabilityChanged"})

    result = make_service(planning, store).evaluate_change(event)

    assert isinstance(result, Ok)
    assert result.value.kind == "unknown"
    assert result.value.reason == "event type not handled yet"
    assert planning.find_calls == []
    assert store.processed("simulation", RUN_ID) == set()


def test_two_closed_segments_used_by_the_same_plan_give_one_disruption_and_one_replan(
    example, affected_work, store
):
    planning = FakePlanning(
        work_by_segment={SEGMENT: affected_work, SECOND_SEGMENT: affected_work}
    )
    data = make_data(example)
    data["payload"]["changes"].append(
        data["payload"]["changes"][0] | {"segmentKey": SECOND_SEGMENT}
    )
    data["affected"]["segmentKeys"] = [SEGMENT, SECOND_SEGMENT]

    result = make_service(planning, store).evaluate_change(
        EventEnvelope.model_validate(data)
    )

    assert isinstance(result, Ok)
    assert result.value.kind == "created"
    (disruption,) = store.disruptions("simulation", RUN_ID)
    assert disruption.segment_keys == [SEGMENT, SECOND_SEGMENT]
    assert [p.plan_id for p in disruption.affected_plans] == [PLAN_ID]
    assert [scope.segment_keys for scope in planning.find_calls] == [
        [SEGMENT],
        [SECOND_SEGMENT],
    ]
    assert len(planning.replan_calls) == 1


def test_closed_segment_without_cause_is_unknown_and_event_is_not_processed(
    example, planning, store
):
    data = make_data(example)
    data["payload"]["changes"][0]["cause"] = None

    result = make_service(planning, store).evaluate_change(
        EventEnvelope.model_validate(data)
    )

    assert isinstance(result, Ok)
    assert result.value.kind == "unknown"
    assert result.value.reason
    assert store.disruptions("simulation", RUN_ID) == []
    assert planning.replan_calls == []
    assert store.processed("simulation", RUN_ID) == set()


def test_invalid_payload_is_rejected_without_calling_c05(example, planning, store):
    data = make_data(example)
    del data["payload"]["snapshotVersion"]

    result = make_service(planning, store).evaluate_change(
        EventEnvelope.model_validate(data)
    )

    assert isinstance(result, Failure)
    assert result.kind == "Invalid"
    assert planning.find_calls == []
    assert store.processed("simulation", RUN_ID) == set()


def test_failed_replan_request_keeps_the_disruption_with_replan_pending(
    example, planning, store
):
    planning.replan_unavailable = True

    result = make_service(planning, store).evaluate_change(make_event(example))

    assert isinstance(result, Ok)
    assert result.value.kind == "created"
    assert result.value.replan_requests == []
    assert result.value.reason
    (disruption,) = store.disruptions("simulation", RUN_ID)
    (plan,) = disruption.affected_plans
    assert (plan.replan_requested, plan.replan_outcome) == (False, None)
    assert store.processed("simulation", RUN_ID) == {EVENT_ID}


def test_failed_replan_request_is_sent_again_on_redelivery_once_c05_is_back(
    example, planning, store
):
    service = make_service(planning, store)
    event = make_event(example)
    planning.replan_unavailable = True
    first = service.evaluate_change(event).value

    planning.replan_unavailable = False
    again = service.evaluate_change(event).value
    after = service.evaluate_change(event).value

    assert first.retry_later is True
    assert (again.kind, again.retry_later) == ("duplicate", False)
    assert len(again.replan_requests) == 1
    (disruption,) = store.disruptions("simulation", RUN_ID)
    (plan,) = disruption.affected_plans
    assert (plan.replan_requested, plan.replan_outcome) == (True, "PlanSaved")
    assert after.replan_requests == []
    first_key = planning.replan_calls[0][0].key
    assert [request.key for request, _ in planning.replan_calls] == [first_key] * 2


def test_redelivery_while_c05_is_still_down_keeps_the_replan_pending(
    example, planning, store
):
    service = make_service(planning, store)
    event = make_event(example)
    planning.replan_unavailable = True
    service.evaluate_change(event)

    again = service.evaluate_change(event).value

    assert (again.kind, again.retry_later) == ("duplicate", True)
    (disruption,) = store.disruptions("simulation", RUN_ID)
    assert disruption.affected_plans[0].replan_requested is False
