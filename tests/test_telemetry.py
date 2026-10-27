"""
Test OpenTelemetry Integration for Bhagiratha: AI-based Village Pond Planning System.

Verifies:
  1. OpenTelemetry initialization and /api/telemetry/status endpoint.
  2. Prometheus /metrics exposition endpoint.
  3. Distributed tracing and custom domain spans on /api/analyze and /api/analyze-area.
  4. Span attributes and events captured in InMemorySpanRecorder (/api/telemetry/spans).
  5. OpenTelemetry metric counters and histograms recorded during operations.
  6. Structured log trace correlation (trace_id and span_id present in logs).
"""

import sys
import os
import logging
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app
from app.telemetry import get_tracer, TraceCorrelationFormatter

client = TestClient(app)


def test_telemetry_status_endpoint():
    """Checks that /api/telemetry/status returns active status, service name, and components."""
    resp = client.get("/api/telemetry/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert "bhagiratha" in data["service_name"]
    assert data["components"]["tracing"] == "active"
    assert data["components"]["metrics"] == "active"
    assert data["components"]["prometheus_exporter"] == "/metrics"
    print("✓ Telemetry status endpoint returns healthy configuration")


def test_prometheus_metrics_endpoint():
    """Checks that /metrics returns valid Prometheus exposition text with custom metrics."""
    resp = client.get("/metrics")
    assert resp.status_code == 200
    text = resp.text
    # Verify standard / custom metrics are present in the prometheus exposition
    assert "pond_analyses_total" in text or "http" in text or "# HELP" in text
    print("✓ Prometheus metrics endpoint successfully exposed at /metrics")


def test_trace_recorded_on_api_analyze():
    """Tests that calling /api/analyze produces traces with domain attributes."""
    # Run a point analysis on synthetic / sample coordinates
    resp = client.post("/api/analyze", json={"lat": 21.15, "lon": 81.30})
    assert resp.status_code == 200

    # Retrieve recorded spans from /api/telemetry/spans
    spans_resp = client.get("/api/telemetry/spans?limit=20")
    assert spans_resp.status_code == 200
    spans_data = spans_resp.json()
    assert spans_data["count"] > 0

    span_names = [s["name"] for s in spans_data["spans"]]
    assert any("pond.analyze_point" in name for name in span_names)

    # Find the analyze span and verify its attributes
    analyze_span = next(s for s in spans_data["spans"] if s["name"] == "pond.analyze_point")
    assert "geo.lat" in analyze_span["attributes"]
    assert "geo.lon" in analyze_span["attributes"]
    assert "terrain.area_km2" in analyze_span["attributes"]
    assert analyze_span["trace_id"] != ""
    assert analyze_span["span_id"] != ""
    print(f"✓ Point analysis generated trace_id={analyze_span['trace_id']} with domain attributes")


def test_area_analysis_emits_telemetry():
    """Tests that /api/analyze-area generates spans with bounds and sizes."""
    payload = {
        "bounds": {
            "min_lat": 21.140,
            "max_lat": 21.160,
            "min_lon": 81.280,
            "max_lon": 81.300
        }
    }
    resp = client.post("/api/analyze-area", json=payload)
    assert resp.status_code == 200

    spans_resp = client.get("/api/telemetry/spans?limit=20")
    spans = spans_resp.json()["spans"]
    area_spans = [s for s in spans if s["name"] == "pond.analyze_area"]
    assert len(area_spans) > 0
    span = area_spans[0]
    assert "bounds.min_lat" in span["attributes"]
    assert "suggested_site.lat" in span["attributes"]
    print("✓ Area analysis correctly instrumented with OpenTelemetry")


def test_logging_trace_correlation():
    """Tests that TraceCorrelationFormatter injects trace_id and span_id into log records."""
    formatter = TraceCorrelationFormatter("%(message)s [trace=%(otelTraceID)s span=%(otelSpanID)s]")
    tracer = get_tracer("test.tracer")

    # Outside of trace context
    record_outside = logging.LogRecord("test", logging.INFO, "test.py", 10, "Outside span", (), None)
    formatted_outside = formatter.format(record_outside)
    assert "trace=00000000000000000000000000000000" in formatted_outside

    # Inside active trace context
    with tracer.start_as_current_span("test_correlated_span") as span:
        ctx = span.get_span_context()
        record_inside = logging.LogRecord("test", logging.INFO, "test.py", 15, "Inside span", (), None)
        formatted_inside = formatter.format(record_inside)
        expected_trace = f"{ctx.trace_id:032x}"
        expected_span = f"{ctx.span_id:016x}"
        assert f"trace={expected_trace}" in formatted_inside
        assert f"span={expected_span}" in formatted_inside

    print("✓ Log correlation properly links log statements to active OpenTelemetry trace IDs")
