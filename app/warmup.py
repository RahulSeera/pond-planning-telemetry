"""
Terrain pipeline warm-up.

The first flow-routing call JIT-compiles PySheds' Numba kernels (~14 s on the
1-CPU container). PySheds caches the compiled kernels on disk (cache=True, in
pysheds/__pycache__), so later processes only load them.

Compiling from scratch costs a lot of memory. Doing it inside the server (whose
baseline is ~300 MB) pushes it past the container's 512 MiB limit and the kernel
OOM-kills it. So start_server.sh runs this module as a separate, short-lived
process first (`python -m app.warmup`) whenever that cache is cold, and the
server's own warm-up then just loads the cached kernels.
"""

import os
import tempfile
import time


def warm_up_pipeline():
    try:
        from app.schemas import BoundingBox
        from app.modules.area_analysis import generate_terrain_for_bounds
        from app.modules.catchment import find_pond_site, catchment_to_geojson
        from app.modules.dem_from_contours import contours_to_grid
        from app.memory_guard import release_memory
        t0 = time.time()
        tmp_dir = tempfile.mkdtemp(prefix="warmup_dem_")
        tif = os.path.join(tmp_dir, "warmup.tif")
        generate_terrain_for_bounds(BoundingBox(min_lat=21.0, max_lat=21.01, min_lon=81.0, max_lon=81.01), tif, resolution=40)
        res = find_pond_site(tif)
        catchment_to_geojson(res["grid"], res["catchment_mask"])
        contours_to_grid([{"elevation": float(z), "coordinates": [(81.0 + 0.001 * i, 21.0 + 0.001 * z) for i in range(5)]} for z in range(5)])
        os.remove(tif)
        os.rmdir(tmp_dir)
        release_memory()
        print(f"Warm-up complete in {time.time() - t0:.1f}s (terrain kernels compiled, memory trimmed).", flush=True)
        return True
    except Exception as e:
        from app.memory_guard import release_memory
        release_memory()
        print(f"NOTICE: warm-up skipped ({e}).", flush=True)
        return False


if __name__ == "__main__":
    raise SystemExit(0 if warm_up_pipeline() else 1)
