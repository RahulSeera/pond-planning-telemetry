#!/usr/bin/env bash
# ==============================================================================
# stop_telemetry.sh — Stops Jaeger and Prometheus containers
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_DIR"

if docker compose version >/dev/null 2>&1; then
    docker compose -f docker-compose.telemetry.yml down
elif command -v docker-compose >/dev/null 2>&1; then
    docker-compose -f docker-compose.telemetry.yml down
fi

# Also remove any standalone containers started via plain docker run
docker rm -f bhagiratha-jaeger bhagiratha-prometheus 2>/dev/null || true

echo "OpenTelemetry stack stopped."
