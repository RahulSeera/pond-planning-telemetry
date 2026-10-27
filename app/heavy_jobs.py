"""
Admission control for CPU/memory-heavy analysis work.

The evaluation container (stu72_sys3) has a 1-CPU cgroup quota and a 512 MiB
memory limit. One contour analysis adds ~25-100 MB on top of a ~300 MB server;
two at once exceeded the limit and the kernel OOM-killed the server during
stress testing. Running heavy jobs in parallel also buys no throughput on one
CPU. So heavy jobs run one at a time (BHAGIRATHA_HEAVY_SLOTS, default 1) in a
worker thread; others wait in a queue, and a request that has waited longer
than QUEUE_TIMEOUT_S is rejected with HTTP 503 + Retry-After instead of piling
up. Light requests (health, cache hits, search) never enter the queue.
"""

import asyncio
import gc
import os
import time

from app.telemetry import get_tracer, METRICS
from app.memory_guard import release_memory, is_memory_critical

HEAVY_SLOTS = max(1, int(os.environ.get("BHAGIRATHA_HEAVY_SLOTS", "1")))
QUEUE_TIMEOUT_S = float(os.environ.get("BHAGIRATHA_QUEUE_TIMEOUT_S", "120"))

# One semaphore per event loop (the test client creates several loops).
_semaphores: dict[int, asyncio.Semaphore] = {}
_waiting = 0


class ServerBusyError(Exception):
    """Raised when a heavy job could not get a slot within QUEUE_TIMEOUT_S or memory is critical."""


def _semaphore() -> asyncio.Semaphore:
    loop_id = id(asyncio.get_running_loop())
    if loop_id not in _semaphores:
        _semaphores[loop_id] = asyncio.Semaphore(HEAVY_SLOTS)
    return _semaphores[loop_id]


def queue_depth() -> int:
    """Number of heavy jobs currently waiting for a slot (reported by /api/health)."""
    return _waiting


async def run_heavy(fn, *args):
    """Run fn(*args) in a worker thread once a heavy-job slot is free and memory is safe."""
    global _waiting
    tracer = get_tracer("bhagiratha.heavy_jobs")
    fn_name = getattr(fn, "__name__", str(fn))

    with tracer.start_as_current_span(f"heavy_job.{fn_name}") as span:
        span.set_attribute("heavy_job.function", fn_name)
        span.set_attribute("heavy_job.queue_waiting", _waiting)

        # Proactive memory circuit breaker: if container memory is near ceiling, shed load
        if is_memory_critical(threshold_pct=0.88):
            span.set_attribute("heavy_job.rejected_oom_protection", True)
            raise ServerBusyError("Server memory is currently high; request deferred to protect system stability. Please retry in 10s.")

        sem = _semaphore()
        _waiting += 1
        try:
            await asyncio.wait_for(sem.acquire(), timeout=QUEUE_TIMEOUT_S)
        except asyncio.TimeoutError:
            span.set_attribute("heavy_job.timeout", True)
            raise ServerBusyError(f"Server busy: no analysis slot free within {QUEUE_TIMEOUT_S:.0f} s.")
        finally:
            _waiting -= 1

        try:
            t0 = time.time()
            result = await asyncio.to_thread(fn, *args)
            duration = time.time() - t0
            span.set_attribute("heavy_job.duration_seconds", round(duration, 3))
            if METRICS and METRICS.heavy_jobs_duration:
                METRICS.heavy_jobs_duration.record(duration, {"function": fn_name})
            return result
        finally:
            sem.release()
            # Immediately reclaim Python heap and force glibc malloc_trim
            # so memory is returned directly to the Linux kernel/cgroup.
            release_memory()
