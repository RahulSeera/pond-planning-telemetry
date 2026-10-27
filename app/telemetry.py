"""
OpenTelemetry Instrumentation & Observability Layer for Bhagiratha: AI-based Village Pond Planning System.

Provides:
  1. Distributed Tracing:
     - Auto-instrumentation: FastAPI, HTTPX (OpenTopography / Open-Meteo / Nominatim), SQLAlchemy.
     - Custom domain spans: catchment delineation, rainfall aggregation, pond sizing, contour parsing.
     - In-memory ring buffer of recent spans for instant UI/API inspection.
     - OTLP HTTP trace export (to Jaeger, SigNoz, Otel Collector on port 4318).
  2. Metrics:
     - Domain counters & histograms: analyses count, catchment area, runoff, pond depth/capacity, suitability score.
     - Prometheus exporter mounted at /metrics.
     - OTLP HTTP metrics exporter.
  3. Logging:
     - Correlated structured logs injecting active trace_id and span_id into log records.
     - Optional OTLP log exporter.
"""

import collections
import logging
import os
import socket
import time
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from prometheus_client import make_asgi_app

# OpenTelemetry Tracing
import opentelemetry.trace as trace
from opentelemetry.trace import Status, StatusCode, Tracer
from opentelemetry.sdk.trace import TracerProvider, ReadableSpan, SpanProcessor
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

# OpenTelemetry Metrics
import opentelemetry.metrics as metrics
from opentelemetry.metrics import Meter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

# OpenTelemetry Logging & Semantic Conventions
from opentelemetry.sdk.resources import Resource, SERVICE_NAME, SERVICE_VERSION
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor

# Global references
_TRACER_PROVIDER: Optional[TracerProvider] = None
_METER_PROVIDER: Optional[MeterProvider] = None
_PROMETHEUS_READER: Optional[PrometheusMetricReader] = None
_SPAN_RECORDER: Optional["InMemorySpanRecorder"] = None

MAX_RECORDED_SPANS = 50


class InMemorySpanRecorder(SpanProcessor):
    """
    Maintains a thread-safe ring buffer of recent spans for real-time
    in-app inspection, UI telemetry dashboards, and verification tests.
    """

    def __init__(self, capacity: int = MAX_RECORDED_SPANS):
        self.capacity = capacity
        self.spans: collections.deque = collections.deque(maxlen=capacity)

    def on_end(self, span: ReadableSpan) -> None:
        try:
            ctx = span.get_span_context()
            trace_id = f"{ctx.trace_id:032x}" if ctx.trace_id else ""
            span_id = f"{ctx.span_id:016x}" if ctx.span_id else ""
            parent_id = f"{span.parent.span_id:016x}" if span.parent and span.parent.span_id else None

            duration_ms = 0.0
            if span.end_time and span.start_time:
                duration_ms = round((span.end_time - span.start_time) / 1_000_000, 2)

            events_data = []
            for ev in span.events:
                events_data.append({
                    "name": ev.name,
                    "timestamp": ev.timestamp,
                    "attributes": dict(ev.attributes) if ev.attributes else {},
                })

            rec = {
                "trace_id": trace_id,
                "span_id": span_id,
                "parent_span_id": parent_id,
                "name": span.name,
                "duration_ms": duration_ms,
                "status": span.status.status_code.name if span.status else "UNSET",
                "status_description": span.status.description if span.status else None,
                "attributes": dict(span.attributes) if span.attributes else {},
                "events": events_data,
                "start_time_ns": span.start_time,
                "end_time_ns": span.end_time,
            }
            self.spans.appendleft(rec)
        except Exception:
            pass

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True

    def get_recent_spans(self, limit: int = 50) -> List[Dict[str, Any]]:
        return list(self.spans)[:limit]

    def clear(self) -> None:
        self.spans.clear()


# ---------------------------------------------------------------------------
# Setup and Configuration
# ---------------------------------------------------------------------------

def _create_resource() -> Resource:
    service_name = os.getenv("OTEL_SERVICE_NAME", "bhagiratha-pond-planning")
    service_version = os.getenv("OTEL_SERVICE_VERSION", "1.0.0")
    environment = os.getenv("OTEL_ENVIRONMENT", "production")

    return Resource.create({
        SERVICE_NAME: service_name,
        SERVICE_VERSION: service_version,
        "deployment.environment": environment,
        "host.name": socket.gethostname(),
        "telemetry.sdk.language": "python",
    })


def setup_telemetry(app: FastAPI, engine: Optional[Any] = None) -> Dict[str, Any]:
    """
    Initializes OpenTelemetry Tracing, Metrics, Logging, and Auto-instrumentation.
    Mounts Prometheus /metrics and registers diagnostic endpoints.
    """
    global _TRACER_PROVIDER, _METER_PROVIDER, _PROMETHEUS_READER, _SPAN_RECORDER

    resource = _create_resource()
    otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318").rstrip("/")
    enable_otlp = os.getenv("OTEL_ENABLE_OTLP", "true").lower() in ("true", "1", "yes")
    console_export = os.getenv("OTEL_CONSOLE_EXPORT", "false").lower() in ("true", "1", "yes")

    # 1. Tracing Setup
    tracer_provider = TracerProvider(resource=resource)
    _SPAN_RECORDER = InMemorySpanRecorder(capacity=MAX_RECORDED_SPANS)
    tracer_provider.add_span_processor(_SPAN_RECORDER)

    if enable_otlp and otlp_endpoint:
        try:
            otlp_trace_exporter = OTLPSpanExporter(
                endpoint=f"{otlp_endpoint}/v1/traces",
                timeout=3,
            )
            tracer_provider.add_span_processor(BatchSpanProcessor(otlp_trace_exporter, max_export_batch_size=32))
        except Exception as e:
            logging.getLogger("bhagiratha.telemetry").warning(f"Could not initialize OTLP trace exporter: {e}")

    if console_export:
        tracer_provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(tracer_provider)
    _TRACER_PROVIDER = tracer_provider

    # 2. Metrics Setup
    _PROMETHEUS_READER = PrometheusMetricReader()
    metric_readers = [_PROMETHEUS_READER]

    if enable_otlp and otlp_endpoint:
        try:
            otlp_metric_exporter = OTLPMetricExporter(
                endpoint=f"{otlp_endpoint}/v1/metrics",
                timeout=3,
            )
            otlp_metric_reader = PeriodicExportingMetricReader(
                otlp_metric_exporter,
                export_interval_millis=15000,
            )
            metric_readers.append(otlp_metric_reader)
        except Exception as e:
            logging.getLogger("bhagiratha.telemetry").warning(f"Could not initialize OTLP metric exporter: {e}")

    meter_provider = MeterProvider(resource=resource, metric_readers=metric_readers)
    metrics.set_meter_provider(meter_provider)
    _METER_PROVIDER = meter_provider

    # 3. Logging Setup & Correlation
    _setup_logging(resource=resource, otlp_endpoint=otlp_endpoint if enable_otlp else None)

    # 4. Framework Auto-instrumentation
    try:
        HTTPXClientInstrumentor().instrument()
    except Exception as e:
        logging.getLogger("bhagiratha.telemetry").debug(f"HTTPX instrumentor notice: {e}")

    if engine is not None:
        try:
            SQLAlchemyInstrumentor().instrument(engine=engine, service_name=resource.attributes.get(SERVICE_NAME))
        except Exception as e:
            logging.getLogger("bhagiratha.telemetry").debug(f"SQLAlchemy instrumentor notice: {e}")

    # Instrument FastAPI app
    try:
        FastAPIInstrumentor.instrument_app(
            app,
            tracer_provider=tracer_provider,
            meter_provider=meter_provider,
            excluded_urls="metrics,static,favicon.ico,api/telemetry",
        )
    except Exception as e:
        logging.getLogger("bhagiratha.telemetry").debug(f"FastAPI instrumentor notice: {e}")

    # 5. Prometheus Scrape Endpoints (supports both /metrics and /metrics/ with zero redirects)
    @app.get("/metrics", include_in_schema=False)
    @app.get("/metrics/", include_in_schema=False)
    def prometheus_metrics_endpoint():
        from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
        from fastapi.responses import Response
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # 6. Initialize Domain Metric Instruments
    _init_domain_metrics()

    # 7. Register Telemetry Diagnostic Endpoints
    _register_telemetry_routes(app)

    logger = logging.getLogger("bhagiratha")
    logger.info("OpenTelemetry successfully initialized (Traces, Metrics, Logs active)")

    return {
        "status": "initialized",
        "service_name": resource.attributes.get(SERVICE_NAME),
        "otlp_endpoint": otlp_endpoint if enable_otlp else "disabled",
        "prometheus_mounted": True,
    }


class TraceCorrelationFormatter(logging.Formatter):
    """
    Safely injects OpenTelemetry trace_id and span_id into log records
    even when logging occurs outside of an active trace context or before instrumentation.
    """
    def format(self, record: logging.LogRecord) -> str:
        span = trace.get_current_span()
        ctx = span.get_span_context() if span else None
        if ctx and ctx.is_valid:
            record.otelTraceID = f"{ctx.trace_id:032x}"
            record.otelSpanID = f"{ctx.span_id:016x}"
        else:
            if not hasattr(record, "otelTraceID"):
                record.otelTraceID = "00000000000000000000000000000000"
            if not hasattr(record, "otelSpanID"):
                record.otelSpanID = "0000000000000000"
        if not hasattr(record, "otelServiceName"):
            record.otelServiceName = "bhagiratha"
        return super().format(record)


def _setup_logging(resource: Resource, otlp_endpoint: Optional[str] = None) -> None:
    """Configures root and app loggers with trace correlation and formatters."""
    try:
        LoggingInstrumentor().instrument(set_logging_format=False)
    except Exception:
        pass

    log_format = "[%(asctime)s] [%(levelname)s] [trace_id=%(otelTraceID)s span_id=%(otelSpanID)s] %(name)s: %(message)s"
    formatter = TraceCorrelationFormatter(log_format)

    root_logger = logging.getLogger()
    if not root_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        root_logger.addHandler(handler)
    else:
        for h in root_logger.handlers:
            h.setFormatter(formatter)

    root_logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Domain Metric Instruments
# ---------------------------------------------------------------------------

class DomainMetrics:
    def __init__(self):
        self.meter: Optional[Meter] = None
        self.analyses_total: Any = None
        self.analysis_duration: Any = None
        self.catchment_area: Any = None
        self.runoff_volume: Any = None
        self.pond_capacity: Any = None
        self.pond_depth: Any = None
        self.suitability_score: Any = None
        self.rainfall_requests_total: Any = None
        self.contour_lines: Any = None
        self.cache_operations_total: Any = None
        self.heavy_jobs_duration: Any = None


METRICS = DomainMetrics()


def _init_domain_metrics() -> None:
    global METRICS
    meter = metrics.get_meter("bhagiratha.hydrology", "1.0.0")
    METRICS.meter = meter

    METRICS.analyses_total = meter.create_counter(
        name="pond_analyses_total",
        description="Total number of pond planning analyses performed",
        unit="1",
    )

    METRICS.analysis_duration = meter.create_histogram(
        name="pond_analysis_duration_seconds",
        description="Duration of end-to-end pond planning analysis",
        unit="s",
    )

    METRICS.catchment_area = meter.create_histogram(
        name="pond_catchment_area_km2",
        description="Delineated upstream catchment basin area",
        unit="km2",
    )

    METRICS.runoff_volume = meter.create_histogram(
        name="pond_runoff_volume_m3",
        description="Estimated monsoon seasonal runoff volume",
        unit="m3",
    )

    METRICS.pond_capacity = meter.create_histogram(
        name="pond_recommended_capacity_m3",
        description="Recommended pond reservoir volume",
        unit="m3",
    )

    METRICS.pond_depth = meter.create_histogram(
        name="pond_recommended_depth_m",
        description="Recommended pond reservoir depth",
        unit="m",
    )

    METRICS.suitability_score = meter.create_histogram(
        name="pond_suitability_score",
        description="Multi-factor pond site suitability score (0-100)",
        unit="1",
    )

    METRICS.rainfall_requests_total = meter.create_counter(
        name="rainfall_requests_total",
        description="Historical rainfall data requests and data source origin",
        unit="1",
    )

    METRICS.contour_lines = meter.create_histogram(
        name="contour_lines_parsed_total",
        description="Number of contour lines parsed from uploaded KML/KMZ surveys",
        unit="1",
    )

    METRICS.cache_operations_total = meter.create_counter(
        name="cache_operations_total",
        description="Geospatial response cache hits and misses",
        unit="1",
    )

    METRICS.heavy_jobs_duration = meter.create_histogram(
        name="heavy_job_duration_seconds",
        description="Execution duration of CPU-intensive hydrology jobs",
        unit="s",
    )


def get_tracer(name: str = "bhagiratha") -> Tracer:
    """Returns an active OpenTelemetry tracer."""
    return trace.get_tracer(name, "1.0.0")


def get_meter(name: str = "bhagiratha") -> Meter:
    """Returns an active OpenTelemetry meter."""
    return metrics.get_meter(name, "1.0.0")


# ---------------------------------------------------------------------------
# Telemetry HTTP Endpoints
# ---------------------------------------------------------------------------

def _register_telemetry_routes(app: FastAPI) -> None:

    @app.get("/api/telemetry/status", include_in_schema=True)
    async def get_telemetry_status():
        """Returns OpenTelemetry runtime configuration, active exporters, and status."""
        endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
        recent_count = len(_SPAN_RECORDER.spans) if _SPAN_RECORDER else 0
        return {
            "status": "healthy",
            "service_name": os.getenv("OTEL_SERVICE_NAME", "bhagiratha-pond-planning"),
            "service_version": os.getenv("OTEL_SERVICE_VERSION", "1.0.0"),
            "environment": os.getenv("OTEL_ENVIRONMENT", "production"),
            "components": {
                "tracing": "active",
                "metrics": "active",
                "logging_correlation": "active",
                "prometheus_exporter": "/metrics",
                "otlp_endpoint": endpoint,
            },
            "spans_buffered": recent_count,
            "max_buffered_spans": MAX_RECORDED_SPANS,
        }

    @app.get("/api/telemetry/spans", include_in_schema=True)
    async def get_recent_spans(limit: int = 50):
        """Returns recently collected OpenTelemetry spans for live inspection and VIVA demonstration."""
        if not _SPAN_RECORDER:
            return {"spans": [], "count": 0}
        spans = _SPAN_RECORDER.get_recent_spans(limit=min(limit, 100))
        return {
            "spans": spans,
            "count": len(spans),
        }
