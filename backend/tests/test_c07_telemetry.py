from types import SimpleNamespace

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from app.components.c07_disruption_detection.detection import DetectionOutcome
from app.components.c07_disruption_detection.telemetry import C07Telemetry
from app.components.c07_disruption_detection.worker import c07_handler
from app.shared.contracts import EventEnvelope
from app.shared.result import Failure, Ok


@pytest.fixture
def telemetry():
    spans = InMemorySpanExporter()
    metrics = InMemoryMetricReader()
    recorder = C07Telemetry(
        "http://unused",
        span_processor=SimpleSpanProcessor(spans),
        metric_reader=metrics,
    )
    try:
        yield recorder, spans, metrics
    finally:
        recorder.shutdown()


@pytest.mark.parametrize(
    ("result_kind", "retryable", "disposition"),
    [
        ("created", False, "ack"),
        ("unknown", True, "retry"),
        ("Invalid", False, "reject"),
        ("Unavailable", True, "retry"),
    ],
)
def test_telemetry_preserves_disposition_and_records_evaluation(
    telemetry, example, result_kind, retryable, disposition
):
    recorder, spans, metrics = telemetry
    envelope = EventEnvelope.model_validate(example("city-state-updated-event.json"))
    if result_kind in ("created", "unknown"):
        result = Ok(
            DetectionOutcome(
                kind=result_kind, event_id=envelope.event_id, retryable=retryable
            )
        )
    else:
        result = Failure(result_kind, "test failure")

    def evaluate(event):
        assert event is envelope
        return result

    detection = SimpleNamespace(evaluate_change=evaluate)
    assert c07_handler(detection)(envelope) == disposition
    assert c07_handler(detection, telemetry=recorder)(envelope) == disposition

    (span,) = spans.get_finished_spans()
    assert span.name == "c07.evaluate_change"
    assert span.attributes["correlationId"] == str(envelope.correlation_id)
    assert span.attributes["eventId"] == str(envelope.event_id)
    assert span.attributes["eventType"] == envelope.event_type
    assert span.attributes["scope"] == envelope.scope
    assert span.attributes["runId"] == str(envelope.run_id)
    assert span.attributes["outcome"] == result_kind
    assert span.attributes["retryable"] is retryable

    exported = {
        metric.name: metric.data.data_points[0]
        for resource in metrics.get_metrics_data().resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }
    events = exported["ulop.c07.evaluate_change.events"]
    duration = exported["ulop.c07.evaluate_change.duration"]
    assert events.value == duration.count == 1
    assert (
        events.attributes
        == duration.attributes
        == {
            "outcome": result_kind,
            "retryable": retryable,
        }
    )


def test_telemetry_records_exception_and_preserves_propagation(telemetry, example):
    recorder, spans, _ = telemetry
    payload = example("city-state-updated-event.json")
    payload.update(scope="live", runId=None)
    envelope = EventEnvelope.model_validate(payload)
    error = RuntimeError("test evaluation crashed")

    def evaluate(event):
        raise error

    detection = SimpleNamespace(evaluate_change=evaluate)
    for configured in (None, recorder):
        with pytest.raises(RuntimeError) as raised:
            c07_handler(detection, telemetry=configured)(envelope)
        assert raised.value is error

    (span,) = spans.get_finished_spans()
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes["outcome"] == "exception"
    assert span.attributes["retryable"] is True
    assert span.attributes["scope"] == "live"
    assert "runId" not in span.attributes
    (exception,) = span.events
    assert exception.name == "exception"
    assert exception.attributes["exception.type"] == "RuntimeError"
    assert exception.attributes["exception.message"] == str(error)
    assert "raise error" in exception.attributes["exception.stacktrace"]
