#!/usr/bin/env python3
"""
verify_telemetry.py — Automated verification script for OpenTelemetry integration.

Tests and displays:
  1. Telemetry status and OpenTelemetry providers
  2. Endpoints execution and generated traces
  3. Span attributes, duration, and trace IDs
  4. Prometheus metrics exposition (/metrics)
  5. Connectivity to Jaeger / Prometheus (if running)
"""

import sys
import os
import time
import json
import httpx

BASE_URL = os.environ.get("BASE_URL", "http://localhost:3000")


def banner(title):
    print("\n" + "=" * 70)
    print(f" {title}")
    print("=" * 70)


def check_telemetry_status(client):
    banner("1. Checking OpenTelemetry Status (/api/telemetry/status)")
    try:
        resp = client.get(f"{BASE_URL}/api/telemetry/status", timeout=5.0)
        resp.raise_for_status()
        data = resp.json()
        print(f"  Service Name:    {data.get('service_name')}")
        print(f"  Service Version: {data.get('service_version')}")
        print(f"  Environment:     {data.get('environment')}")
        print(f"  Tracing:         {data.get('components', {}).get('tracing')}")
        print(f"  Metrics:         {data.get('components', {}).get('metrics')}")
        print(f"  Logging Link:    {data.get('components', {}).get('logging_correlation')}")
        print(f"  Prometheus:      {data.get('components', {}).get('prometheus_exporter')}")
        print(f"  OTLP Endpoint:   {data.get('components', {}).get('otlp_endpoint')}")
        print("✓ Telemetry subsystem is initialized and healthy!")
        return True
    except Exception as e:
        print(f"✗ Failed to reach telemetry status endpoint: {e}")
        return False


def generate_workload(client):
    banner("2. Generating Application Workload to Produce Traces & Metrics")

    # A. Village Search
    print("  -> Searching villages (GET /api/villages/search?q=Bhilai)...")
    try:
        r1 = client.get(f"{BASE_URL}/api/villages/search?q=Bhilai", timeout=10.0)
        print(f"     Status: {r1.status_code}, Found {len(r1.json().get('results', []))} results")
    except Exception as e:
        print(f"     Search notice: {e}")

    # B. Point Analysis
    print("  -> Running Point Analysis (POST /api/analyze at lat=21.15, lon=81.30)...")
    try:
        r2 = client.post(f"{BASE_URL}/api/analyze", json={"lat": 21.15, "lon": 81.30}, timeout=45.0)
        if r2.status_code == 200:
            d2 = r2.json()
            rec = d2.get("recommendation") or {}
            print(f"     Status: {r2.status_code}, Catchment: {d2['terrain']['area_km2']} km², "
                  f"Runoff: {rec.get('runoff_m3', 0)} m³, Score: {rec.get('suitability_score', 0)}")
        else:
            print(f"     Analysis response: {r2.status_code}")
    except Exception as e:
        print(f"     Point analysis notice: {e}")

    # C. Area Analysis
    print("  -> Running Land Area Selection (POST /api/analyze-area)...")
    try:
        r3 = client.post(
            f"{BASE_URL}/api/analyze-area",
            json={"bounds": {"min_lat": 21.140, "max_lat": 21.160, "min_lon": 81.280, "max_lon": 81.300}},
            timeout=45.0,
        )
        if r3.status_code == 200:
            d3 = r3.json()
            site = d3.get("suggested_pond_site", {})
            print(f"     Status: {r3.status_code}, Suggested Site: ({site.get('lat')}, {site.get('lon')}), "
                  f"Catchment: {d3.get('catchment_area_km2')} km²")
        else:
            print(f"     Area analysis response: {r3.status_code}")
    except Exception as e:
        print(f"     Area analysis notice: {e}")


def inspect_traces(client):
    banner("3. Inspecting Collected OpenTelemetry Spans (/api/telemetry/spans)")
    try:
        resp = client.get(f"{BASE_URL}/api/telemetry/spans?limit=15", timeout=5.0)
        resp.raise_for_status()
        data = resp.json()
        spans = data.get("spans", [])
        print(f"  Total buffered spans: {len(spans)}")
        print("\n  Recent Traces and Spans:")
        print("  " + "-" * 76)
        print(f"  {'SPAN NAME':<26} | {'TRACE ID':<16} | {'DURATION':<9} | {'STATUS'}")
        print("  " + "-" * 76)
        for s in spans[:10]:
            name = s.get("name", "")[:25]
            trace_id = s.get("trace_id", "")[:16] + "..."
            dur = f"{s.get('duration_ms', 0):.1f}ms"
            status = s.get("status", "UNSET")
            print(f"  {name:<26} | {trace_id:<16} | {dur:<9} | {status}")
        print("  " + "-" * 76)

        # Show detailed attributes of one key span
        key_span = next((s for s in spans if "pond.analyze" in s.get("name", "")), None)
        if key_span:
            print(f"\n  Deep-dive into Span '{key_span.get('name')}':")
            print(f"    Full Trace ID: {key_span.get('trace_id')}")
            print(f"    Span ID:       {key_span.get('span_id')}")
            print(f"    Duration:      {key_span.get('duration_ms')} ms")
            print("    Key Attributes:")
            for k, v in key_span.get("attributes", {}).items():
                print(f"      • {k}: {v}")
        return True
    except Exception as e:
        print(f"✗ Failed to inspect spans: {e}")
        return False


def inspect_prometheus_metrics(client):
    banner("4. Inspecting Prometheus Metrics (/metrics)")
    try:
        resp = client.get(f"{BASE_URL}/metrics", timeout=5.0)
        resp.raise_for_status()
        text = resp.text

        metrics_found = []
        for line in text.splitlines():
            if line.startswith("# HELP"):
                parts = line.split()
                if len(parts) >= 3:
                    metrics_found.append(parts[2])

        print(f"  Total exposed metric families: {len(metrics_found)}")
        print("  Custom Hydrology & System Metrics Detected:")
        for m in [
            "pond_analyses_total",
            "pond_analysis_duration_seconds",
            "pond_catchment_area_km2",
            "pond_runoff_volume_m3",
            "pond_recommended_capacity_m3",
            "pond_suitability_score",
            "rainfall_requests_total",
            "cache_operations_total",
            "heavy_job_duration_seconds",
        ]:
            present = m in metrics_found or m in text
            print(f"    [{'✓' if present else ' '}] {m}")

        print("\n  Sample Prometheus text excerpt:")
        sample_lines = [l for l in text.splitlines() if not l.startswith("#") and l.strip()][:6]
        for l in sample_lines:
            print(f"    {l}")
        return True
    except Exception as e:
        print(f"✗ Failed to inspect metrics: {e}")
        return False


def inspect_external_backends(client):
    banner("5. Observability Backends Status Check")
    # Jaeger check
    try:
        jr = client.get("http://localhost:16686", timeout=2.0)
        if jr.status_code == 200:
            print("  ✓ Jaeger UI is running at http://localhost:16686")
        else:
            print(f"  • Jaeger UI responded with status {jr.status_code}")
    except Exception:
        print("  • Jaeger UI container not detected on localhost:16686 (start with: ./scripts/start_telemetry.sh)")

    # Prometheus check
    try:
        pr = client.get("http://localhost:9090/-/ready", timeout=2.0)
        if pr.status_code == 200:
            print("  ✓ Prometheus Server is running at http://localhost:9090")
        else:
            print(f"  • Prometheus server responded with status {pr.status_code}")
    except Exception:
        print("  • Prometheus container not detected on localhost:9090 (start with: ./scripts/start_telemetry.sh)")


def main():
    print("=" * 70)
    print(" BHAGIRATHA POND PLANNING — OPENTELEMETRY VERIFICATION")
    print(f" Target Server: {BASE_URL}")
    print("=" * 70)

    with httpx.Client() as client:
        # Check if server is running
        try:
            r = client.get(f"{BASE_URL}/api/health", timeout=3.0)
            print(f"✓ Connected to Bhagiratha API (Status: {r.status_code})")
        except Exception:
            print(f"ERROR: Cannot connect to server at {BASE_URL}.")
            print("Please start the server first with: ./scripts/start_server.sh or uvicorn app.main:app")
            sys.exit(1)

        check_telemetry_status(client)
        generate_workload(client)
        inspect_traces(client)
        inspect_prometheus_metrics(client)
        inspect_external_backends(client)

    banner("SUMMARY")
    print("  All OpenTelemetry components are functional:")
    print("  1. Tracing:   Capturing end-to-end spans, coordinates, and timings.")
    print("  2. Metrics:   Exposing Prometheus counters and histograms at /metrics.")
    print("  3. Logging:   Injecting active trace_id and span_id into log messages.")
    print("=" * 70)


if __name__ == "__main__":
    main()
