#!/usr/bin/env bash
# ==============================================================================
# watchdog_daemon.sh — Continuous background watchdog for 100% uptime
# ==============================================================================

PROJECT_DIR="/home/student/pond-project"
WATCHDOG_SCRIPT="$PROJECT_DIR/scripts/watchdog.sh"

while true; do
    if [ -f "$WATCHDOG_SCRIPT" ]; then
        bash "$WATCHDOG_SCRIPT" >/dev/null 2>&1 || true
    fi
    sleep 20
done
