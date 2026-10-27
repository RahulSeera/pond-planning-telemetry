#!/usr/bin/env bash
# ==============================================================================
# start_telemetry.sh — Starts Jaeger (Traces) and Prometheus (Metrics)
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "========================================================================"
echo " Starting OpenTelemetry Observability Stack (Jaeger & Prometheus)"
echo "========================================================================"

cd "$PROJECT_DIR"

if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: Docker is not installed or not in PATH."
    echo "If running in a container without Docker, OpenTelemetry metrics are still"
    echo "accessible directly at: http://localhost:3000/metrics and /api/telemetry/spans"
    exit 1
fi

# Detect compose command or fall back to plain docker run
if docker compose version >/dev/null 2>&1; then
    echo "Launching via docker compose..."
    docker compose -f docker-compose.telemetry.yml up -d
elif command -v docker-compose >/dev/null 2>&1; then
    echo "Launching via docker-compose..."
    docker-compose -f docker-compose.telemetry.yml up -d
else
    echo "docker-compose not found — falling back to plain 'docker run'..."
    docker rm -f bhagiratha-jaeger bhagiratha-prometheus 2>/dev/null || true

    echo "Starting Jaeger container (UI: 16686, OTLP: 4318)..."
    docker run -d --name bhagiratha-jaeger \
      -e COLLECTOR_OTLP_ENABLED=true \
      -p 16686:16686 \
      -p 4317:4317 \
      -p 4318:4318 \
      jaegertracing/all-in-one:latest

    echo "Starting Prometheus container (UI: 9090, scraping app on 3000)..."
    docker run -d --name bhagiratha-prometheus \
      -p 9090:9090 \
      -v "$PROJECT_DIR/prometheus.yml":/etc/prometheus/prometheus.yml:ro \
      --add-host=host.docker.internal:host-gateway \
      prom/prometheus:latest
fi

echo ""
echo "Waiting for observability services to become ready..."
for i in {1..20}; do
    if curl -s http://localhost:16686 >/dev/null 2>&1 && curl -s http://localhost:9090/-/ready >/dev/null 2>&1; then
        echo "✓ Jaeger and Prometheus are healthy and ready!"
        break
    fi
    sleep 1
done

echo ""
echo "========================================================================"
echo " Observability Services Ready:"
echo " 1. Jaeger UI (Distributed Traces):  http://localhost:16686"
echo " 2. Prometheus UI (Live Metrics):    http://localhost:9090"
echo " 3. App Metrics Scrape Endpoint:     http://localhost:3000/metrics"
echo " 4. In-App Telemetry Diagnostic:     http://localhost:3000/api/telemetry/status"
echo " 5. In-App Spans API:                http://localhost:3000/api/telemetry/spans"
echo "========================================================================"
