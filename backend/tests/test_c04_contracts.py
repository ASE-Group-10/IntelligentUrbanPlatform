"""Contract tests for C04 Delivery Management schemas and examples."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.components.c04_delivery_management.contracts import (
    Delivery,
    DeliveryEvent,
    SubmitDeliveryRequest,
    UpdateDeliveryRequest,
)
from app.shared.contracts import EventEnvelope, RunScopeSelector

C04_EXAMPLES = [
    (SubmitDeliveryRequest, "submit-delivery-request.json"),
    (Delivery, "delivery-submitted.json"),
    (DeliveryEvent, "delivery-submitted-event.json"),
    (DeliveryEvent, "delivery-progress-changed-event.json"),
    (EventEnvelope, "delivery-submitted-event.json"),
    (EventEnvelope, "delivery-progress-changed-event.json"),
]


@pytest.mark.parametrize(
    ("model", "name"),
    C04_EXAMPLES,
    ids=[f"{m.__name__}:{n}" for m, n in C04_EXAMPLES],
)
def test_c04_example_round_trips(model, name, example):
    data = example(name)
    parsed = model.model_validate(data)
    assert parsed.model_dump(mode="json", by_alias=True, exclude_unset=True) == data


def test_simulation_get_requests_example_validates(example):
    data = example("simulation-get-requests.json")
    scope = RunScopeSelector.model_validate(data["request"]["runScope"])
    assert scope.scope == "simulation"
    assert scope.run_id is not None
    delivery = Delivery.model_validate(data["result"]["ok"][0])
    assert delivery.to_wire() == data["result"]["ok"][0]


def test_unknown_field_is_rejected(example):
    data = example("submit-delivery-request.json") | {"extraField": True}
    with pytest.raises(ValidationError):
        SubmitDeliveryRequest.model_validate(data)


def test_time_window_must_be_utc_and_ordered(example):
    non_utc = deepcopy(example("submit-delivery-request.json"))
    non_utc["timeWindow"]["start"] = "2026-11-20T09:00:00+01:00"
    with pytest.raises(ValidationError):
        SubmitDeliveryRequest.model_validate(non_utc)

    reversed_window = deepcopy(example("submit-delivery-request.json"))
    reversed_window["timeWindow"]["end"] = reversed_window["timeWindow"]["start"]
    with pytest.raises(ValidationError):
        SubmitDeliveryRequest.model_validate(reversed_window)


def test_cargo_and_constraints_validation(example):
    bad_weight = deepcopy(example("submit-delivery-request.json"))
    bad_weight["cargo"]["weightKg"] = 0
    with pytest.raises(ValidationError):
        SubmitDeliveryRequest.model_validate(bad_weight)

    bool_parcels = deepcopy(example("submit-delivery-request.json"))
    bool_parcels["cargo"]["parcelCount"] = True
    with pytest.raises(ValidationError):
        SubmitDeliveryRequest.model_validate(bool_parcels)

    empty_vehicles = deepcopy(example("submit-delivery-request.json"))
    empty_vehicles["constraints"]["vehicleKinds"] = []
    with pytest.raises(ValidationError):
        SubmitDeliveryRequest.model_validate(empty_vehicles)

    bad_temp = deepcopy(example("submit-delivery-request.json"))
    bad_temp["constraints"]["temperatureRangeC"] = {"minimum": 10, "maximum": 2}
    with pytest.raises(ValidationError):
        SubmitDeliveryRequest.model_validate(bad_temp)


def test_run_scope_selector_enforces_partition():
    assert RunScopeSelector.model_validate({"scope": "live"}).run_id is None
    with pytest.raises(ValidationError):
        RunScopeSelector.model_validate({"scope": "simulation", "runId": None})
    with pytest.raises(ValidationError):
        RunScopeSelector.model_validate(
            {"scope": "live", "runId": "c3f1a2b4-5d6e-4f70-8a9b-0c1d2e3f4a5b"}
        )


def test_update_request_requires_positive_expected_version(example):
    payload = deepcopy(example("submit-delivery-request.json")) | {"expectedVersion": 0}
    with pytest.raises(ValidationError):
        UpdateDeliveryRequest.model_validate(payload)
