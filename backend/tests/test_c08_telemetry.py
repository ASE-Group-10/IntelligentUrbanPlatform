from types import SimpleNamespace
from uuid import uuid4

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from app.components.c08_allocation.telemetry import C08Telemetry
from app.components.c08_allocation.worker import make_consumer
from app.shared.contracts import EventEnvelope
from tests.test_resources import setup  # noqa: F401


@pytest.fixture
def telemetry():
    spans = InMemorySpanExporter()
    metrics = InMemoryMetricReader()
    recorder = C08Telemetry(
        "http://unused",
        span_processor=SimpleSpanProcessor(spans),
        metric_reader=metrics,
    )
    try:
        yield recorder, spans, metrics
    finally:
        recorder.shutdown()


@pytest.mark.parametrize(
    ("outcome", "disposition"),
    [("allocated", "ack"), ("duplicate", "ack"), ("plan unavailable", "retry")],
)
def test_worker_preserves_disposition(telemetry, example, outcome, disposition):
    recorder, spans, metrics = telemetry
    event = EventEnvelope.model_validate(example("plan-ready-event.json"))
    store = SimpleNamespace(consume=lambda event: outcome)
    for enabled in (None, recorder):
        consumer = make_consumer(store, "amqp://localhost", telemetry=enabled)
        assert consumer._handler(event) == disposition
    (span,) = spans.get_finished_spans()
    assert span.attributes["correlationId"] == str(event.correlation_id)
    assert span.attributes["eventId"] == str(event.event_id)
    assert span.attributes["scope"] == event.scope
    assert span.attributes["disposition"] == disposition
    for resource in metrics.get_metrics_data().resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                assert set(metric.data.data_points[0].attributes) == {
                    "operation",
                    "outcome",
                }


def test_worker_records_invalid_event_and_exception(telemetry, example):
    recorder, spans, _ = telemetry
    event = EventEnvelope.model_validate(example("plan-ready-event.json"))
    error = RuntimeError("allocation write failed")

    def fail(event):
        raise error

    consumer = make_consumer(
        SimpleNamespace(consume=fail), "amqp://localhost", telemetry=recorder
    )
    with pytest.raises(RuntimeError) as raised:
        consumer._handler(event)
    assert raised.value is error
    invalid = event.model_copy(update={"event_type": "Unexpected"})
    assert consumer._handler(invalid) == "reject"
    crashed, rejected = spans.get_finished_spans()
    assert crashed.status.status_code is StatusCode.ERROR
    assert crashed.attributes["disposition"] == "exception"
    assert crashed.events[0].attributes["exception.message"] == str(error)
    assert rejected.attributes["disposition"] == "reject"


def test_http_auth_failure_and_unrelated_route(telemetry, setup):  # noqa: F811
    recorder, spans, _ = telemetry
    client, _ = setup
    client.app.state.c08_telemetry = recorder
    correlation = str(uuid4())
    response = client.post(
        f"/api/v1/driver-offers/{uuid4()}/response",
        json={"response": "accept"},
        headers={"X-Correlation-ID": correlation},
    )
    assert response.status_code == 401
    (span,) = spans.get_finished_spans()
    assert span.attributes["http.route"] == "/api/v1/driver-offers/{offerId}/response"
    assert span.attributes["http.response.status_code"] == 401
    assert span.attributes["correlationId"] == correlation
    client.get("/openapi.json")
    assert len(spans.get_finished_spans()) == 1
    client.app.state.c08_telemetry = None


def test_http_success_keeps_allocation_correlation(telemetry, setup, monkeypatch):  # noqa: F811
    from app.components.c08_allocation import router
    from app.integrations.identity import authenticate

    recorder, spans, _ = telemetry
    client, _ = setup
    correlation, assignment = str(uuid4()), str(uuid4())
    result = {
        "code": "assigned",
        "correlationId": correlation,
        "assignmentRef": assignment,
        "acceptanceStatus": "accepted",
    }
    client.app.state.c08_telemetry = recorder
    client.app.dependency_overrides[authenticate] = lambda: SimpleNamespace(
        roles=("driver",)
    )
    monkeypatch.setattr(
        router, "store", lambda request: SimpleNamespace(respond=lambda *args: result)
    )
    response = client.post(
        f"/api/v1/driver-offers/{uuid4()}/response",
        headers={"Idempotency-Key": "synthetic-offer"},
        json={"response": "accept"},
    )
    assert response.status_code == 200
    assert response.headers["X-Correlation-ID"] == correlation
    (span,) = spans.get_finished_spans()
    assert span.attributes["correlationId"] == correlation
    assert span.attributes["assignmentRef"] == assignment
    assert span.attributes["outcome"] == "accepted"
    assert span.attributes["scope"] == "live"
    client.app.state.c08_telemetry = None
    client.app.dependency_overrides.clear()
