"""Contract tests: every shared example parses, round-trips and rejects bad input."""

import pytest
from pydantic import BaseModel, ValidationError

from app.components.c06_city_monitoring.contracts import (
    CitySnapshot,
    CitySnapshotQuery,
    CityStateUpdated,
    IngestionReceipt,
    Observation,
)
from app.components.c07_disruption_detection.contracts import (
    AffectedWork,
    ChangeScope,
    Disruption,
    OperationalDisruption,
    ReplanOutcome,
    ReplanRequest,
    ResourceAvailabilityChanged,
    parse_payload,
)
from app.shared.contracts import EventEnvelope

EXAMPLES: list[tuple[type[BaseModel], str]] = [
    (EventEnvelope, "event_envelope.city_state_updated.json"),
    (CityStateUpdated, "city_state_updated.payload.json"),
    (Observation, "observation.closure.json"),
    (IngestionReceipt, "ingestion_receipt.json"),
    (CitySnapshotQuery, "city_snapshot.query.json"),
    (CitySnapshot, "city_snapshot.json"),
    (ChangeScope, "change_scope.json"),
    (AffectedWork, "affected_work.json"),
    (ReplanRequest, "replan_request.json"),
    (ReplanOutcome, "replan_outcome.plan_saved.json"),
    (Disruption, "disruption.json"),
    (ResourceAvailabilityChanged, "resource_availability_changed.payload.json"),
    (OperationalDisruption, "operational_disruption.payload.json"),
]


@pytest.mark.parametrize(("model", "name"), EXAMPLES, ids=[n for _, n in EXAMPLES])
def test_example_round_trips(model, name, example):
    data = example(name)
    parsed = model.model_validate(data)
    assert parsed.model_dump(mode="json", by_alias=True, exclude_unset=True) == data


def test_unknown_field_is_rejected(example):
    data = example("city_state_updated.payload.json") | {"snapshotVerison": 1}
    with pytest.raises(ValidationError):
        CityStateUpdated.model_validate(data)


def test_parse_payload_returns_typed_city_state_updated(example):
    envelope = EventEnvelope.model_validate(
        example("event_envelope.city_state_updated.json")
    )
    payload = parse_payload(envelope)
    assert isinstance(payload, CityStateUpdated)
    assert payload.changes[0].status == "closed"


def test_parse_payload_rejects_unknown_event_type(example):
    data = example("event_envelope.city_state_updated.json")
    envelope = EventEnvelope.model_validate(data | {"eventType": "Unheard"})
    with pytest.raises(ValueError):
        parse_payload(envelope)


class TestEnvelopeScope:
    def test_simulation_requires_run_id(self, example):
        data = example("event_envelope.city_state_updated.json") | {"runId": None}
        with pytest.raises(ValidationError):
            EventEnvelope.model_validate(data)

    def test_live_must_not_carry_run_id(self, example):
        data = example("event_envelope.city_state_updated.json") | {"scope": "live"}
        with pytest.raises(ValidationError):
            EventEnvelope.model_validate(data)

    def test_live_without_run_id_is_valid(self, example):
        data = example("event_envelope.city_state_updated.json") | {
            "scope": "live",
            "runId": None,
        }
        assert EventEnvelope.model_validate(data).scope == "live"


class TestTimestamps:
    @pytest.mark.parametrize(
        "value", ["2026-11-20T08:14:30", "2026-11-20T09:14:30+01:00"]
    )
    def test_naive_or_non_utc_timestamp_is_rejected(self, value, example):
        data = example("observation.closure.json") | {"observedAt": value}
        with pytest.raises(ValidationError):
            Observation.model_validate(data)

    def test_utc_timestamp_serialises_with_z_suffix(self, example):
        observation = Observation.model_validate(example("observation.closure.json"))
        dumped = observation.model_dump(mode="json", by_alias=True)
        assert dumped["observedAt"] == "2026-11-20T08:14:30Z"


def test_segment_status_outside_enum_is_rejected(example):
    data = example("city_state_updated.payload.json")
    data["changes"][0]["status"] = "blocked"
    with pytest.raises(ValidationError):
        CityStateUpdated.model_validate(data)


def test_snapshot_version_must_be_positive(example):
    data = example("city_state_updated.payload.json") | {"snapshotVersion": 0}
    with pytest.raises(ValidationError):
        CityStateUpdated.model_validate(data)


def test_first_city_state_update_may_replace_version_zero(example):
    data = example("city_state_updated.payload.json") | {
        "snapshotVersion": 1,
        "previousVersion": 0,
    }
    assert CityStateUpdated.model_validate(data).previous_version == 0


class TestCitySnapshotQuery:
    def test_requires_segment_keys_or_bbox(self):
        with pytest.raises(ValidationError):
            CitySnapshotQuery.model_validate({"scope": {}})

    def test_rejects_both_segment_keys_and_bbox(self):
        scope = {"segmentKeys": ["seg:1"], "bbox": [-6.3, 53.3, -6.2, 53.4]}
        with pytest.raises(ValidationError):
            CitySnapshotQuery.model_validate({"scope": scope})


class TestObservationTarget:
    def test_requires_a_target(self, example):
        data = example("observation.closure.json") | {"target": {}}
        with pytest.raises(ValidationError):
            Observation.model_validate(data)

    def test_accepts_lat_lon_target(self, example):
        data = example("observation.closure.json") | {
            "target": {"lat": 53.3438, "lon": -6.2546}
        }
        assert Observation.model_validate(data).target.lat == 53.3438


def test_change_scope_must_name_something():
    with pytest.raises(ValidationError):
        ChangeScope.model_validate({"segmentKeys": []})


class TestReplanRequest:
    def test_key_must_match_plan_disruption_and_version(self, example):
        data = example("replan_request.json")
        data["trigger"]["disruptionVersion"] = 2
        with pytest.raises(ValidationError):
            ReplanRequest.model_validate(data)

    def test_build_derives_idempotency_key(self, example):
        expected = ReplanRequest.model_validate(example("replan_request.json"))
        built = ReplanRequest.build(plan_id=expected.plan_id, trigger=expected.trigger)
        assert built == expected


class TestReplanOutcome:
    def test_plan_saved_requires_new_version_and_decision(self):
        with pytest.raises(ValidationError):
            ReplanOutcome.model_validate({"status": "PlanSaved"})

    def test_no_change_needs_nothing_else(self):
        assert ReplanOutcome.model_validate({"status": "NoChange"}).status == "NoChange"


@pytest.mark.parametrize(
    "key", ["4471023", "seg:", "seg:abc", "seg:-1", "edge:4471023"]
)
def test_segment_key_must_be_seg_osm_way_id(key, example):
    data = example("change_scope.json") | {"segmentKeys": [key]}
    with pytest.raises(ValidationError):
        ChangeScope.model_validate(data)


def test_replan_is_synchronous_so_publication_pending_is_rejected():
    with pytest.raises(ValidationError):
        ReplanOutcome.model_validate({"status": "PublicationPending"})
