"""C08 event processing and HTTP operation telemetry."""

import re
from time import perf_counter

from opentelemetry.trace import SpanKind, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from app.shared.telemetry import OtlpTelemetry

HTTP_OPERATIONS = (
    ("GET", r"/api/v1/vehicle-assignments", "/api/v1/vehicle-assignments"),
    ("POST", r"/api/v1/vehicle-assignments", "/api/v1/vehicle-assignments"),
    (
        "POST",
        r"/api/v1/driver-offers/[^/]+/response",
        "/api/v1/driver-offers/{offerId}/response",
    ),
)


class C08Telemetry(OtlpTelemetry):
    def __init__(self, endpoint, **options):
        super().__init__(endpoint, "urban-logistics-c08", **options)
        self._tracer = self.traces.get_tracer(__name__)
        meter = self.metrics.get_meter(__name__)
        self._operations = meter.create_counter("ulop.c08.operations")
        self._duration = meter.create_histogram("ulop.c08.duration", unit="ms")
        self._propagator = TraceContextTextMapPropagator()

    def record(self, started, attributes):
        self._operations.add(1, attributes)
        self._duration.record((perf_counter() - started) * 1000, attributes)

    def consume(self, envelope, handle):
        started = perf_counter()
        disposition = "exception"
        with self._tracer.start_as_current_span(
            "c08.plan_ready",
            kind=SpanKind.CONSUMER,
            attributes={
                "messaging.system": "rabbitmq",
                "messaging.operation.type": "process",
                "eventId": str(envelope.event_id),
                "correlationId": str(envelope.correlation_id),
                "eventType": envelope.event_type,
                "scope": envelope.scope,
            },
        ) as span:
            if envelope.run_id is not None:
                span.set_attribute("runId", str(envelope.run_id))
            try:
                disposition = handle(envelope)
                return disposition
            finally:
                span.set_attribute("disposition", disposition)
                if disposition != "ack":
                    span.set_status(StatusCode.ERROR)
                self.record(
                    started, {"operation": "plan_ready", "outcome": disposition}
                )

    async def observe_request(self, request, call_next):
        route = next(
            (
                template
                for method, pattern, template in HTTP_OPERATIONS
                if request.method == method and re.fullmatch(pattern, request.url.path)
            ),
            None,
        )
        if route is None:
            return await call_next(request)
        started = perf_counter()
        status = 500
        with self._tracer.start_as_current_span(
            f"{request.method} {route}",
            context=self._propagator.extract(request.headers),
            kind=SpanKind.SERVER,
            attributes={"http.request.method": request.method, "http.route": route},
        ) as span:
            try:
                response = await call_next(request)
                status = response.status_code
                return response
            finally:
                span.set_attribute("http.response.status_code", status)
                span.set_attribute("correlationId", request.state.correlation_id)
                # Validation in the router owns scope/run authorization.
                scope = getattr(request.state, "c08_scope", None)
                if scope is not None:
                    span.set_attribute("scope", scope)
                run_id = getattr(request.state, "c08_run_id", None)
                if run_id is not None:
                    span.set_attribute("runId", str(run_id))
                outcome = getattr(request.state, "c08_outcome", None)
                if outcome is not None:
                    span.set_attribute("outcome", outcome)
                    span.set_attribute("assignmentRef", request.state.c08_assignment)
                if status >= 500:
                    span.set_status(StatusCode.ERROR)
                self.record(
                    started,
                    {
                        "operation": f"{request.method} {route}",
                        "http.response.status_code": status,
                    },
                )
