"""Shared OTLP/HTTP exporters for instrumented backend processes."""

from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import Counter, Histogram, MeterProvider
from opentelemetry.sdk.metrics.export import (
    AggregationTemporality,
    MetricReader,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


class OtlpTelemetry:
    def __init__(
        self,
        endpoint: str,
        service: str,
        *,
        span_processor: SpanProcessor | None = None,
        metric_reader: MetricReader | None = None,
        **attributes: str,
    ):
        endpoint = endpoint.rstrip("/")
        resource = Resource.create({"service.name": service, **attributes})
        self.traces = TracerProvider(resource=resource, shutdown_on_exit=False)
        if span_processor is None:
            span_processor = BatchSpanProcessor(
                OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces", timeout=3)
            )
        self.traces.add_span_processor(span_processor)
        if metric_reader is None:
            metric_exporter = OTLPMetricExporter(
                endpoint=f"{endpoint}/v1/metrics",
                timeout=3,
                preferred_temporality={
                    Counter: AggregationTemporality.DELTA,
                    Histogram: AggregationTemporality.DELTA,
                },
            )
            metric_reader = PeriodicExportingMetricReader(
                metric_exporter, export_interval_millis=10000
            )
        self.metrics = MeterProvider(
            resource=resource, metric_readers=[metric_reader], shutdown_on_exit=False
        )

    def shutdown(self):
        try:
            self.traces.shutdown()
        finally:
            self.metrics.shutdown()
