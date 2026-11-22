"""OpenTelemetry for the C08 driver offer response."""

from contextlib import contextmanager
from time import perf_counter

from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import Counter, Histogram, MeterProvider
from opentelemetry.sdk.metrics.export import (
    AggregationTemporality,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from sqlalchemy.exc import SQLAlchemyError


class C08Telemetry:
    def __init__(self, endpoint: str, version: str):
        endpoint = endpoint.rstrip("/")
        resource = Resource.create(
            {"service.name": "urban-logistics-backend", "service.version": version}
        )
        self._traces = TracerProvider(resource=resource, shutdown_on_exit=False)
        self._traces.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces", timeout=3)
            )
        )
        metric_exporter = OTLPMetricExporter(
            endpoint=f"{endpoint}/v1/metrics",
            timeout=3,
            preferred_temporality={
                Counter: AggregationTemporality.DELTA,
                Histogram: AggregationTemporality.DELTA,
            },
        )
        self._metrics = MeterProvider(
            resource=resource,
            metric_readers=[
                PeriodicExportingMetricReader(
                    metric_exporter, export_interval_millis=10000
                )
            ],
            shutdown_on_exit=False,
        )
        self._propagator = TraceContextTextMapPropagator()
        self._tracer = self._traces.get_tracer(__name__)
        meter = self._metrics.get_meter(__name__)
        self._requests = meter.create_counter("ulop.c08.offer_response.requests")
        self._duration = meter.create_histogram(
            "ulop.c08.offer_response.duration", unit="ms"
        )
        self._db_duration = meter.create_histogram(
            "ulop.c08.offer_response.db.duration", unit="ms"
        )

    async def observe_request(self, request, call_next, route: str):
        started = perf_counter()
        status = 500
        with self._tracer.start_as_current_span(
            f"POST {route}",
            context=self._propagator.extract(request.headers),
            kind=SpanKind.SERVER,
            attributes={"http.request.method": "POST", "http.route": route},
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                response = await call_next(request)
                status = response.status_code
                return response
            finally:
                attributes = {"http.response.status_code": status}
                span.set_attributes(attributes)
                if status >= 500:
                    span.set_status(StatusCode.ERROR)
                self._requests.add(1, attributes)
                self._duration.record((perf_counter() - started) * 1000, attributes)

    @contextmanager
    def transaction(self):
        started = perf_counter()
        with self._tracer.start_as_current_span(
            "c08.offer_response.transaction",
            attributes={"db.system.name": "postgresql"},
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                yield
            except SQLAlchemyError:
                span.set_status(StatusCode.ERROR)
                raise
            finally:
                self._db_duration.record((perf_counter() - started) * 1000)

    def shutdown(self):
        try:
            self._traces.shutdown()
        finally:
            self._metrics.shutdown()
