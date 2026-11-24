"""Trace and measure C07 event evaluations."""

from collections.abc import Callable
from time import perf_counter

from opentelemetry.sdk.metrics.export import MetricReader
from opentelemetry.sdk.trace import SpanProcessor
from opentelemetry.trace import SpanKind, StatusCode

from app.components.c07_disruption_detection.detection import DetectionOutcome
from app.shared.contracts import EventEnvelope
from app.shared.result import Ok, Result
from app.shared.telemetry import OtlpTelemetry


class C07Telemetry(OtlpTelemetry):
    def __init__(
        self,
        endpoint: str,
        *,
        span_processor: SpanProcessor | None = None,
        metric_reader: MetricReader | None = None,
    ):
        super().__init__(
            endpoint,
            "urban-logistics-c07",
            span_processor=span_processor,
            metric_reader=metric_reader,
        )
        self._tracer = self.traces.get_tracer(__name__)
        meter = self.metrics.get_meter(__name__)
        self._evaluations = meter.create_counter("ulop.c07.evaluate_change.events")
        self._duration = meter.create_histogram(
            "ulop.c07.evaluate_change.duration", unit="ms"
        )
        self._replans = meter.create_counter("ulop.c07.replans.accepted")

    def evaluate(
        self,
        envelope: EventEnvelope,
        evaluate_change: Callable[[EventEnvelope], Result[DetectionOutcome]],
    ) -> Result[DetectionOutcome]:
        started = perf_counter()
        outcome = "exception"
        retryable = True
        replans = 0
        with self._tracer.start_as_current_span(
            "c07.evaluate_change",
            kind=SpanKind.CONSUMER,
            attributes={
                "messaging.system": "rabbitmq",
                "messaging.operation.type": "process",
                "messaging.message.id": str(envelope.event_id),
                "correlationId": str(envelope.correlation_id),
                "eventId": str(envelope.event_id),
                "eventType": envelope.event_type,
                "scope": envelope.scope,
            },
            record_exception=True,
            set_status_on_exception=False,
        ) as span:
            if envelope.run_id is not None:
                span.set_attribute("runId", str(envelope.run_id))
            try:
                result = evaluate_change(envelope)
                if isinstance(result, Ok):
                    outcome = result.value.kind
                    retryable = result.value.retryable
                    replans = len(result.value.replan_requests)
                else:
                    outcome = result.kind
                    retryable = outcome != "Invalid"
                return result
            finally:
                attributes = {"outcome": outcome, "retryable": retryable}
                span.set_attributes(attributes)
                span.set_attribute("c07.replans.accepted", replans)
                if retryable or outcome == "Invalid":
                    span.set_status(StatusCode.ERROR)
                self._evaluations.add(1, attributes)
                self._duration.record((perf_counter() - started) * 1000, attributes)
                if replans:
                    self._replans.add(replans)
