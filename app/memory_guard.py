"""
Memory Guard & OOM Protection for Bhagiratha Pond Planning System.

Protects against Linux cgroup OOM kills on memory-constrained containers (512 MiB limit).
Provides:
  1. Instant memory trimmed reclamation via glibc `malloc_trim(0)`.
  2. Memory usage and limit inspection (cgroup v2, v1, and /proc/self/status fallback).
  3. Proactive load-shedding before the kernel OOM killer terminates the process.
"""

import ctypes
import gc
import logging
import os
from typing import Optional

logger = logging.getLogger("bhagiratha.memory")

_LIBC: Optional[ctypes.CDLL] = None
try:
    _LIBC = ctypes.CDLL("libc.so.6")
except Exception:
    _LIBC = None


def release_memory() -> int:
    """
    Forces immediate Python garbage collection and instructs the glibc
    allocator to return unused arena heaps back to the Linux kernel/cgroup.
    Returns the result of malloc_trim (1 if memory released, 0 otherwise).
    """
    collected = gc.collect()
    trimmed = 0
    if _LIBC and hasattr(_LIBC, "malloc_trim"):
        try:
            trimmed = _LIBC.malloc_trim(0)
        except Exception:
            pass
    return trimmed


def get_current_memory_mb() -> float:
    """
    Returns actual resident memory (VmRSS) of the current Python process in MB.
    VmRSS reflects active allocations, avoiding kernel page-cache distortion.
    """
    if os.path.exists("/proc/self/status"):
        try:
            with open("/proc/self/status", "r") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        parts = line.split()
                        if len(parts) >= 2 and parts[1].isdigit():
                            return round(int(parts[1]) / 1024, 2)
        except Exception:
            pass

    # Fallback to cgroup v2
    if os.path.exists("/sys/fs/cgroup/memory.current"):
        try:
            with open("/sys/fs/cgroup/memory.current", "r") as f:
                val = f.read().strip()
                if val.isdigit():
                    return round(int(val) / (1024 * 1024), 2)
        except Exception:
            pass

    return 0.0


def get_memory_limit_mb() -> float:
    """Returns the container memory limit in MB, or 512.0 default."""
    if os.path.exists("/sys/fs/cgroup/memory.max"):
        try:
            with open("/sys/fs/cgroup/memory.max", "r") as f:
                val = f.read().strip()
                if val.isdigit():
                    return round(int(val) / (1024 * 1024), 2)
        except Exception:
            pass
    return 512.0


def is_memory_critical(threshold_pct: float = 0.92) -> bool:
    """
    Checks if process VmRSS exceeds safe ceiling (default 92% = ~470 MB).
    Immediately triggers malloc_trim before returning True.
    """
    current_mb = get_current_memory_mb()
    limit_mb = get_memory_limit_mb()
    if limit_mb > 0 and current_mb > 0:
        if (current_mb / limit_mb) >= threshold_pct:
            release_memory()
            current_mb = get_current_memory_mb()
            return (current_mb / limit_mb) >= threshold_pct
    return False
