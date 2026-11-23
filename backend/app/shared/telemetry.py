"""Shared OTLP/HTTP exporters for instrumented backend processes."""

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


class OtlpTelemetry:
    def __init__(self, endpoint: str, service: str, **attributes: str):
        endpoint = endpoint.rstrip("/")
        resource = Resource.create({"service.name": service, **attributes})
        self.traces = TracerProvider(resource=resource, shutdown_on_exit=False)
        self.traces.add_span_processor(
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
        self.metrics = MeterProvider(
            resource=resource,
            metric_readers=[
                PeriodicExportingMetricReader(
                    metric_exporter, export_interval_millis=10000
                )
            ],
            shutdown_on_exit=False,
        )

    def shutdown(self):
        try:
            self.traces.shutdown()
        finally:
            self.metrics.shutdown()
