"""
API LAYER — Coordinator for Bhagiratha: AI-based Village Pond Planning System.
Named after the legendary sage-king Bhagiratha who brought celestial waters to earth.

Endpoints:
  1. GET  /                         -> Interactive Leaflet Frontend Web Application
  2. POST /api/analyze             -> Point-based Catchment & Pond Analysis
  3. GET  /api/analyze/{id}        -> Retrieve analysis by ID
  4. POST /api/analyze-area        -> Land Area Selection Analysis (bounding box / polygon)
  5. POST /analyzeContour          -> Contour Map (KML/KMZ) Analysis
  6. GET  /api/villages/search     -> Village Geocoding & Search
  7. GET  /api/villages/{id}/history -> Past analyses for a specific village
  8. GET  /api/analyses/recent     -> Recent global analyses
  9. GET  /api/health              -> Health and uptime check

Docs auto-generated at: /docs
"""

import asyncio
import logging
import os
import time
import json
import tempfile
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Depends, UploadFile, File, Query
from app.telemetry import setup_telemetry, get_tracer, METRICS, Status, StatusCode
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from typing import Optional, List, Dict, Any

from app.schemas import (
    AnalyzeRequest,
    AnalyzeResponse,
    ContourAnalysisResponse,
    PondSite,
    TerrainResult,
    RainfallResult,
    RecommendationResult,
    BoundingBox,
    AnalyzeAreaRequest,
    AreaAnalysisResponse,
    VillageSearchResponse,
    HealthResponse,
)
from app.modules.kml_parser import parse_contours, ContourParseError
from app.modules.dem_from_contours import contours_to_grid, write_grid_to_geotiff, InterpolationError
from app.modules.catchment import find_pond_site, catchment_to_geojson
from app.modules.area_analysis import analyze_land_area
from app.modules.village_search import search_villages
from app.heavy_jobs import run_heavy, ServerBusyError

START_TIME = time.time()
DB_IS_AVAILABLE = False
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
HISTORY_FILE = os.path.join(DATA_DIR, "analyses_history.json")

# In-memory storage buffers for zero-dependency / container-standalone execution.
# Populated from data/analyses_history.json at startup and by every analysis.
IN_MEMORY_ANALYSES: List[Dict[str, Any]] = []
IN_MEMORY_CACHE: Dict[str, Dict[str, Any]] = {}
MAX_HISTORY_RECORDS = 100
MAX_CACHE_ENTRIES = 500


def _load_history_from_file():
    global IN_MEMORY_ANALYSES
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, list) and loaded:
                    IN_MEMORY_ANALYSES = loaded
        except Exception as e:
            print(f"Notice: Could not load history file: {e}")


def _save_history_to_file():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(IN_MEMORY_ANALYSES[:MAX_HISTORY_RECORDS], f, indent=2)
    except Exception as e:
        print(f"Notice: Could not save history file: {e}")


def _record_analysis(request_id: int, village_id, lat: float, lon: float,
                     terrain: TerrainResult, rec: Optional[RecommendationResult]):
    """Adds one analysis to the (bounded) in-memory history and persists it to the JSON file.
    Stores the full sizing result so GET /api/analyze/{id} can return real values later."""
    IN_MEMORY_ANALYSES.insert(0, {
        "request_id": request_id,
        "village_id": village_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "lat": round(lat, 5),
        "lon": round(lon, 5),
        "area_km2": terrain.area_km2,
        "avg_slope": terrain.avg_slope,
        "runoff_m3": rec.runoff_m3 if rec else 0,
        "depth_m": rec.depth_m if rec else None,
        "surface_area_m2": rec.surface_area_m2 if rec else None,
        "capacity_m3": rec.capacity_m3 if rec else None,
        "suitability_score": rec.suitability_score if rec else 0,
    })
    del IN_MEMORY_ANALYSES[MAX_HISTORY_RECORDS:]
    _save_history_to_file()


def _new_request_id() -> int:
    return int(time.time() * 1000) % 1_000_000_000


app = FastAPI(
    title="Bhagiratha: AI-based Village Pond Planning System",
    description="Interactive geospatial system for recommending optimal village pond locations using terrain elevation, D8 catchment delineation, and historical precipitation.",
    version="1.0.0",
)

# Initialize OpenTelemetry (Tracing, Metrics, Logs, Prometheus /metrics)
setup_telemetry(app)
tracer = get_tracer("bhagiratha.api")
logger = logging.getLogger("bhagiratha")

# Mount static files for Leaflet frontend
static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
if not os.path.exists(static_dir):
    os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.exception_handler(ServerBusyError)
async def _server_busy_handler(request, exc):
    return JSONResponse(status_code=503, content={"detail": str(exc)}, headers={"Retry-After": "15"})


@app.on_event("startup")
def on_startup():
    global DB_IS_AVAILABLE
    _load_history_from_file()
    try:
        from app.db.database import engine, Base, is_db_available
        if is_db_available():
            Base.metadata.create_all(bind=engine)
            DB_IS_AVAILABLE = True
            logger.info("✓ Connected to PostgreSQL database and initialized PostGIS tables.")
        else:
            DB_IS_AVAILABLE = False
            logger.warning("NOTICE: PostgreSQL is not reachable. Operating in resilient persistent-file mode.")
    except Exception as e:
        DB_IS_AVAILABLE = False
        logger.warning(f"NOTICE: Database operating in memory-resilient mode ({e}).")

    # Warm up in the background on a tiny synthetic DEM so the first real user
    # request is fast. start_server.sh compiles the Numba kernels beforehand in a
    # separate process (see app/warmup.py), so here they are only loaded from cache.
    import threading
    from app.warmup import warm_up_pipeline
    threading.Thread(target=warm_up_pipeline, daemon=True).start()

    # Background Open-Meteo reachability probe (feeds the rainfall circuit breaker).
    if os.environ.get("BHAGIRATHA_DISABLE_RAINFALL_PROBE") != "1":
        from app.modules.rainfall import probe_loop
        threading.Thread(target=lambda: asyncio.run(probe_loop()), daemon=True).start()

    # Continuous memory management: runs every 25 seconds, aggressively reclaiming
    # heap fragmentation and returning pages to the 512MB Linux cgroup via malloc_trim(0)
    from app.memory_guard import release_memory
    def _memory_reclaim_worker():
        while True:
            time.sleep(25)
            release_memory()
    threading.Thread(target=_memory_reclaim_worker, daemon=True).start()


@app.middleware("http")
async def _memory_guard_middleware(request, call_next):
    response = await call_next(request)
    # Reclaim intermediate NumPy/PySheds memory immediately after heavy GIS calls
    if request.url.path in ("/api/analyze", "/api/analyze-area", "/analyzeContour"):
        from app.memory_guard import release_memory
        release_memory()
    return response


@app.get("/")
async def root():
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {
        "status": "ok",
        "service": "Bhagiratha: AI-based Village Pond Planning System",
        "docs": "/docs",
        "endpoints": ["/api/analyze", "/api/analyze-area", "/analyzeContour", "/api/villages/search"],
    }


def _get_db_lazy():
    """Yields a database session if live, otherwise yields None gracefully."""
    if not DB_IS_AVAILABLE:
        yield None
        return
    try:
        from app.db.database import SessionLocal
        db = SessionLocal()
    except Exception:
        yield None
        return
    # Only session CREATION is guarded above. Exceptions raised by the endpoint
    # itself (e.g. HTTPException 502) are thrown back in at this yield and must
    # propagate — catching them and yielding again crashes the request with a 500.
    try:
        yield db
    finally:
        db.close()


# ---------- Point-based Analysis ----------

@app.post("/api/analyze", response_model=AnalyzeResponse)
async def analyze(req: AnalyzeRequest, db=Depends(_get_db_lazy)):
    from app.modules.terrain import get_catchment, TerrainDataError
    from app.modules.rainfall import get_rainfall_stats, RainfallDataError, fallback_rainfall, BUNDLED_RAINFALL_NOTE_PREFIX
    from app.modules.recommendation import recommend

    warnings: list[str] = []
    cache_key = f"{round(req.lat, 3)},{round(req.lon, 3)}"
    t0 = time.time()

    with tracer.start_as_current_span("pond.analyze_point") as span:
        span.set_attribute("geo.lat", req.lat)
        span.set_attribute("geo.lon", req.lon)
        if req.village_id is not None:
            span.set_attribute("village.id", req.village_id)

        # 1. Check cache (PostgreSQL or In-Memory)
        if db is not None:
            try:
                from app.db.cache import get_cached_response
                cached = get_cached_response(db, req.lat, req.lon)
                if cached is not None:
                    span.set_attribute("cache.hit", True)
                    span.set_attribute("cache.type", "postgresql")
                    if isinstance(cached.get("terrain"), dict) and "area_km2" in cached["terrain"]:
                        span.set_attribute("terrain.area_km2", cached["terrain"]["area_km2"])
                    if METRICS and METRICS.cache_operations_total:
                        METRICS.cache_operations_total.add(1, {"backend": "db", "result": "hit"})
                        METRICS.analyses_total.add(1, {"mode": "point", "status": "cached"})
                    logger.info(f"Point analysis cache HIT (DB): lat={req.lat}, lon={req.lon}")
                    return AnalyzeResponse(**cached)
            except Exception:
                pass
        elif cache_key in IN_MEMORY_CACHE:
            mem_cached = IN_MEMORY_CACHE[cache_key]
            span.set_attribute("cache.hit", True)
            span.set_attribute("cache.type", "in_memory")
            if isinstance(mem_cached.get("terrain"), dict) and "area_km2" in mem_cached["terrain"]:
                span.set_attribute("terrain.area_km2", mem_cached["terrain"]["area_km2"])
            if METRICS and METRICS.cache_operations_total:
                METRICS.cache_operations_total.add(1, {"backend": "memory", "result": "hit"})
                METRICS.analyses_total.add(1, {"mode": "point", "status": "cached"})
            logger.info(f"Point analysis cache HIT (Memory): lat={req.lat}, lon={req.lon}")
            return AnalyzeResponse(**mem_cached)

        span.set_attribute("cache.hit", False)
        if METRICS and METRICS.cache_operations_total:
            METRICS.cache_operations_total.add(1, {"backend": "db" if db else "memory", "result": "miss"})

        # 2. Fire Terrain and Rainfall calls concurrently
        terrain_task = asyncio.create_task(get_catchment(req.lat, req.lon))
        rainfall_task = asyncio.create_task(get_rainfall_stats(req.lat, req.lon))

        # 3. Terrain result
        try:
            terrain_result, terrain_warnings = await terrain_task
            warnings.extend(terrain_warnings)
        except TerrainDataError as e:
            rainfall_task.cancel()
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            if METRICS and METRICS.analyses_total:
                METRICS.analyses_total.add(1, {"mode": "point", "status": "error"})
            logger.error(f"Point analysis terrain error: {e}")
            raise HTTPException(status_code=502, detail=f"Terrain data unavailable: {e}")

        # 4. Rainfall result with graceful fallback
        rainfall_result = None
        try:
            rainfall_result = await rainfall_task
        except RainfallDataError:
            rainfall_result, note = fallback_rainfall(req.lat, req.lon)
            warnings.append(note)

        # 5. Recommendation
        recommendation_result = None
        if rainfall_result is not None:
            recommendation_result = recommend(terrain_result, rainfall_result)

        # 6. Persist to DB or In-Memory buffer
        request_id = _new_request_id()
        if db is not None:
            try:
                from app.db import crud
                db_request = crud.create_analysis_request(db, lat=req.lat, lon=req.lon, village_id=req.village_id)
                request_id = db_request.id
                crud.save_terrain_result(db, request_id, terrain_result)
                if rainfall_result is not None:
                    crud.save_rainfall_result(db, request_id, rainfall_result)
                if recommendation_result is not None:
                    crud.save_recommendation(db, request_id, recommendation_result)
            except Exception as e:
                logger.warning(f"Database write skipped: {e}")

        # Record in in-memory history
        _record_analysis(request_id, req.village_id, req.lat, req.lon, terrain_result, recommendation_result)

        response = AnalyzeResponse(
            request_id=request_id,
            lat=req.lat,
            lon=req.lon,
            terrain=terrain_result,
            rainfall=rainfall_result,
            recommendation=recommendation_result,
            warnings=warnings,
        )

        # Cache only full-quality results
        if all(w.startswith(BUNDLED_RAINFALL_NOTE_PREFIX) for w in warnings):
            if db is not None:
                try:
                    from app.db.cache import set_cached_response
                    set_cached_response(db, req.lat, req.lon, response.model_dump())
                    if METRICS and METRICS.cache_operations_total:
                        METRICS.cache_operations_total.add(1, {"backend": "db", "result": "set"})
                except Exception:
                    pass
            if len(IN_MEMORY_CACHE) >= MAX_CACHE_ENTRIES:
                IN_MEMORY_CACHE.pop(next(iter(IN_MEMORY_CACHE)))
            IN_MEMORY_CACHE[cache_key] = response.model_dump()
            if METRICS and METRICS.cache_operations_total:
                METRICS.cache_operations_total.add(1, {"backend": "memory", "result": "set"})

        duration = time.time() - t0
        span.set_attribute("pond.duration_seconds", round(duration, 3))
        span.set_attribute("terrain.area_km2", terrain_result.area_km2)
        span.set_attribute("terrain.avg_slope", terrain_result.avg_slope)
        if recommendation_result is not None:
            span.set_attribute("recommendation.runoff_m3", recommendation_result.runoff_m3)
            span.set_attribute("recommendation.depth_m", recommendation_result.depth_m)
            span.set_attribute("recommendation.capacity_m3", recommendation_result.capacity_m3)
            span.set_attribute("recommendation.suitability_score", recommendation_result.suitability_score)

        if METRICS:
            if METRICS.analyses_total:
                METRICS.analyses_total.add(1, {"mode": "point", "status": "success"})
            if METRICS.analysis_duration:
                METRICS.analysis_duration.record(duration, {"mode": "point"})
            if METRICS.catchment_area:
                METRICS.catchment_area.record(terrain_result.area_km2, {"mode": "point"})
            if recommendation_result is not None:
                if METRICS.runoff_volume:
                    METRICS.runoff_volume.record(recommendation_result.runoff_m3)
                if METRICS.pond_capacity:
                    METRICS.pond_capacity.record(recommendation_result.capacity_m3)
                if METRICS.pond_depth:
                    METRICS.pond_depth.record(recommendation_result.depth_m)
                if METRICS.suitability_score:
                    METRICS.suitability_score.record(recommendation_result.suitability_score)

        logger.info(
            f"Point analysis succeeded: lat={req.lat}, lon={req.lon}, area={terrain_result.area_km2}km2, "
            f"runoff={recommendation_result.runoff_m3 if recommendation_result else 0}m3, duration={duration:.2f}s"
        )
        return response


@app.get("/api/analyze/{request_id}", response_model=AnalyzeResponse)
async def get_analysis(request_id: int, db=Depends(_get_db_lazy)):
    if db is not None:
        try:
            from app.db import crud
            from geoalchemy2.shape import to_shape
            from shapely.geometry import mapping

            fetched = crud.get_analysis_by_id(db, request_id)
            if fetched is not None:
                point_shape = to_shape(fetched.point)
                return AnalyzeResponse(
                    request_id=fetched.id,
                    lat=point_shape.y,
                    lon=point_shape.x,
                    terrain={
                        "catchment_polygon": mapping(to_shape(fetched.terrain_result.catchment_polygon)) if fetched.terrain_result and fetched.terrain_result.catchment_polygon else {},
                        "area_km2": fetched.terrain_result.area_km2 if fetched.terrain_result else 0,
                        "avg_slope": fetched.terrain_result.avg_slope if fetched.terrain_result else 0,
                    },
                    rainfall={
                        "annual_avg_mm": fetched.rainfall_result.annual_avg_mm,
                        "seasonal": fetched.rainfall_result.seasonal_json,
                        "data_years": fetched.rainfall_result.data_years,
                    } if fetched.rainfall_result else None,
                    recommendation={
                        "runoff_m3": fetched.recommendation.runoff_m3,
                        "depth_m": fetched.recommendation.depth_m,
                        "surface_area_m2": fetched.recommendation.surface_area_m2,
                        "capacity_m3": fetched.recommendation.capacity_m3,
                        "suitability_score": fetched.recommendation.suitability_score,
                    } if fetched.recommendation else None,
                    warnings=[],
                )
        except Exception:
            pass

    # Fallback to in-memory lookup. The history file stores summary figures only
    # (no polygon / rainfall); older records may also lack the sizing fields, in
    # which case the recommendation is left out rather than invented.
    for a in IN_MEMORY_ANALYSES:
        if a.get("request_id") == request_id:
            rec = None
            if a.get("depth_m") is not None:
                rec = RecommendationResult(
                    runoff_m3=a["runoff_m3"], depth_m=a["depth_m"],
                    surface_area_m2=a["surface_area_m2"], capacity_m3=a["capacity_m3"],
                    suitability_score=a["suitability_score"],
                )
            return AnalyzeResponse(
                request_id=request_id,
                lat=a["lat"],
                lon=a["lon"],
                terrain=TerrainResult(catchment_polygon={"type": "Polygon", "coordinates": []},
                                      area_km2=a["area_km2"], avg_slope=a.get("avg_slope") or 0.0),
                rainfall=None,
                recommendation=rec,
                warnings=["Retrieved from file-based history: catchment polygon and rainfall detail are not stored."],
            )

    raise HTTPException(status_code=404, detail="Analysis request not found.")


# ---------- Land Area Selection Analysis ----------

@app.post("/api/analyze-area", response_model=AreaAnalysisResponse)
async def analyze_area_endpoint(req: AnalyzeAreaRequest):
    """
    Analyzes a user-selected land area (bounding box) on the map:
    Discovers optimal natural pond site, delineates catchment,
    and calculates expected water volume that can be collected.
    """
    if req.bounds.min_lat >= req.bounds.max_lat or req.bounds.min_lon >= req.bounds.max_lon:
        raise HTTPException(status_code=422, detail="Bounds must satisfy min_lat < max_lat and min_lon < max_lon.")

    t0 = time.time()
    with tracer.start_as_current_span("pond.analyze_area") as span:
        span.set_attribute("bounds.min_lat", req.bounds.min_lat)
        span.set_attribute("bounds.max_lat", req.bounds.max_lat)
        span.set_attribute("bounds.min_lon", req.bounds.min_lon)
        span.set_attribute("bounds.max_lon", req.bounds.max_lon)

        try:
            res = await analyze_land_area(req.bounds)
        except Exception as e:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            if METRICS and METRICS.analyses_total:
                METRICS.analyses_total.add(1, {"mode": "area", "status": "error"})
            logger.error(f"Area analysis error: {e}")
            raise

        _record_analysis(
            _new_request_id(), None, res.suggested_pond_site.lat, res.suggested_pond_site.lon,
            TerrainResult(catchment_polygon={}, area_km2=res.catchment_area_km2, avg_slope=res.avg_slope_percent),
            res.recommendation,
        )

        duration = time.time() - t0
        span.set_attribute("suggested_site.lat", res.suggested_pond_site.lat)
        span.set_attribute("suggested_site.lon", res.suggested_pond_site.lon)
        span.set_attribute("catchment.area_km2", res.catchment_area_km2)
        span.set_attribute("terrain.avg_slope", res.avg_slope_percent)
        span.set_attribute("recommendation.runoff_m3", res.expected_water_volume_m3)
        span.set_attribute("recommendation.suitability_score", res.recommendation.suitability_score)

        if METRICS:
            if METRICS.analyses_total:
                METRICS.analyses_total.add(1, {"mode": "area", "status": "success"})
            if METRICS.analysis_duration:
                METRICS.analysis_duration.record(duration, {"mode": "area"})
            if METRICS.catchment_area:
                METRICS.catchment_area.record(res.catchment_area_km2, {"mode": "area"})
            if METRICS.runoff_volume:
                METRICS.runoff_volume.record(res.expected_water_volume_m3)
            if METRICS.suitability_score:
                METRICS.suitability_score.record(res.recommendation.suitability_score)

        logger.info(
            f"Area analysis succeeded: site=({res.suggested_pond_site.lat:.4f}, {res.suggested_pond_site.lon:.4f}), "
            f"area={res.catchment_area_km2}km2, score={res.recommendation.suitability_score}, duration={duration:.2f}s"
        )
        return res


# ---------- Contour Map Upload Analysis ----------

@app.post("/analyzeContour", response_model=ContourAnalysisResponse)
async def analyze_contour(contour_map: UploadFile = File(...)):
    """
    Accepts a contour map (.kml or .kmz), auto-discovers optimal pond site via D8
    flow-accumulation, delineates catchment, calculates runoff volume, and sizes the pond.
    """
    warnings: list[str] = []
    filename = contour_map.filename or "uploaded_contour_map"

    if not (filename.lower().endswith(".kml") or filename.lower().endswith(".kmz")):
        raise HTTPException(status_code=400, detail="File must be a .kml or .kmz contour map.")

    file_bytes = await contour_map.read()
    t0 = time.time()

    with tracer.start_as_current_span("pond.analyze_contour") as span:
        span.set_attribute("contour.filename", filename)
        span.set_attribute("contour.size_bytes", len(file_bytes))

        try:
            job = await run_heavy(_contour_job, file_bytes, filename)
        except ContourParseError as e:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            if METRICS and METRICS.analyses_total:
                METRICS.analyses_total.add(1, {"mode": "contour", "status": "error_parse"})
            raise HTTPException(status_code=400, detail=f"Could not parse contour file: {e}")
        except InterpolationError as e:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            if METRICS and METRICS.analyses_total:
                METRICS.analyses_total.add(1, {"mode": "contour", "status": "error_interpolation"})
            raise HTTPException(status_code=422, detail=f"Could not build a terrain grid from this file: {e}")
        except ServerBusyError:
            span.set_attribute("contour.server_busy", True)
            raise
        except Exception as e:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            if METRICS and METRICS.analyses_total:
                METRICS.analyses_total.add(1, {"mode": "contour", "status": "error_server"})
            import traceback
            traceback.print_exc()
            raise HTTPException(status_code=500, detail="Terrain analysis failed on this contour map.")

        contours_count, elevations, result, polygon = job["count"], job["elevations"], job["result"], job["polygon"]

        elev_min = min(elevations) if elevations else 0
        elev_max = max(elevations) if elevations else 0
        span.set_attribute("contour.line_count", contours_count)
        span.set_attribute("elevation.min_m", elev_min)
        span.set_attribute("elevation.max_m", elev_max)
        span.set_attribute("suggested_site.lat", result["pour_lat"])
        span.set_attribute("suggested_site.lon", result["pour_lon"])
        span.set_attribute("catchment.area_km2", result["area_km2"])

        if METRICS and METRICS.contour_lines:
            METRICS.contour_lines.record(contours_count)

        if result.get("touches_edge"):
            warnings.append(
                "The catchment reaches the edge of the uploaded contour map, so the true upstream "
                "area may be larger than reported — treat catchment area and runoff as lower bounds."
            )
        if result["area_km2"] == 0:
            warnings.append(
                "Catchment area came back as zero — the auto-selected point may not sit on a "
                "clear drainage path in this terrain."
            )

        # Step 6: Compute rainfall & recommendation for the auto-discovered pond site
        from app.modules.rainfall import get_rainfall_stats, RainfallDataError, fallback_rainfall
        try:
            rainfall_result = await get_rainfall_stats(result["pour_lat"], result["pour_lon"])
        except RainfallDataError:
            rainfall_result, note = fallback_rainfall(result["pour_lat"], result["pour_lon"])
            warnings.append(note)

        from app.modules.recommendation import recommend
        terrain_res = TerrainResult(
            catchment_polygon=polygon,
            area_km2=result["area_km2"],
            avg_slope=result["avg_slope"],
        )
        rec_result = recommend(terrain_res, rainfall_result)

        _record_analysis(_new_request_id(), None, result["pour_lat"], result["pour_lon"], terrain_res, rec_result)

        duration = time.time() - t0
        span.set_attribute("pond.duration_seconds", round(duration, 3))
        span.set_attribute("recommendation.runoff_m3", rec_result.runoff_m3)
        span.set_attribute("recommendation.suitability_score", rec_result.suitability_score)

        if METRICS:
            if METRICS.analyses_total:
                METRICS.analyses_total.add(1, {"mode": "contour", "status": "success"})
            if METRICS.analysis_duration:
                METRICS.analysis_duration.record(duration, {"mode": "contour"})
            if METRICS.catchment_area:
                METRICS.catchment_area.record(result["area_km2"], {"mode": "contour"})
            if METRICS.runoff_volume:
                METRICS.runoff_volume.record(rec_result.runoff_m3)
            if METRICS.suitability_score:
                METRICS.suitability_score.record(rec_result.suitability_score)

        logger.info(
            f"Contour analysis completed for {filename}: {contours_count} lines, runoff={rec_result.runoff_m3}m3, duration={duration:.2f}s"
        )

        return ContourAnalysisResponse(
            source_filename=filename,
            contour_lines_parsed=contours_count,
            elevation_range_m={"min": elev_min, "max": elev_max},
            suggested_pond_site=PondSite(lat=result["pour_lat"], lon=result["pour_lon"]),
            catchment_area_km2=result["area_km2"],
            avg_slope_percent=result["avg_slope"],
            catchment_polygon=polygon,
            expected_water_volume_m3=rec_result.runoff_m3,
            rainfall=rainfall_result,
            recommendation=rec_result,
            warnings=warnings,
        )


# ---------- Bundled sample data (used by the "Load Sample Demo" button) ----------

SAMPLE_CONTOUR_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   "tests", "sample_data", "contours_1m.kml")


@app.get("/api/sample-contour", include_in_schema=False)
async def sample_contour():
    """Serves the bundled real 1 m contour survey so the demo runs the genuine contour pipeline."""
    if not os.path.exists(SAMPLE_CONTOUR_FILE):
        raise HTTPException(status_code=404, detail="Sample contour file not installed on this server.")
    return FileResponse(SAMPLE_CONTOUR_FILE, media_type="application/vnd.google-earth.kml+xml",
                        filename="contours_1m.kml")


def _contour_job(file_bytes: bytes, filename: str) -> dict:
    """Synchronous contour pipeline, run in a worker thread under run_heavy()."""
    with tracer.start_as_current_span("contour.pipeline") as span:
        span.set_attribute("contour.filename", filename)
        span.set_attribute("contour.size_bytes", len(file_bytes))
        contours = parse_contours(file_bytes, filename)
        elevations = [c["elevation"] for c in contours]
        count = len(contours)
        span.set_attribute("contour.line_count", count)
        span.add_event("contours_parsed", {"count": count})
        grid_data = contours_to_grid(contours)
        span.add_event("grid_interpolated")
        del contours
        tmp_dir = tempfile.mkdtemp(prefix="contour_dem_")
        tif_path = os.path.join(tmp_dir, "dem.tif")
        try:
            write_grid_to_geotiff(grid_data, tif_path)
            del grid_data
            result = find_pond_site(tif_path)
            span.add_event("pond_site_selected", {"pour_lat": result["pour_lat"], "pour_lon": result["pour_lon"]})
            polygon = catchment_to_geojson(result["grid"], result["catchment_mask"])
        finally:
            try:
                if os.path.exists(tif_path):
                    os.remove(tif_path)
                os.rmdir(tmp_dir)
            except OSError:
                pass
        # Keep only what the response needs; drop the grid/flow rasters now.
        slim = {k: result[k] for k in ("pour_lat", "pour_lon", "area_km2", "avg_slope", "touches_edge")}
        return {"count": count, "elevations": elevations, "result": slim, "polygon": polygon}


# ---------- Village Search & History ----------

@app.get("/api/villages/search", response_model=VillageSearchResponse)
async def search_village_endpoint(q: str = Query("", description="Village or district name")):
    with tracer.start_as_current_span("village.search") as span:
        span.set_attribute("search.query", q)
        results = await search_villages(q)
        span.set_attribute("search.results_count", len(results))
        return VillageSearchResponse(query=q, results=results)


@app.get("/api/villages/{village_id}/history")
async def get_village_history_endpoint(village_id: int, db=Depends(_get_db_lazy)):
    if db is not None:
        try:
            from app.db import crud
            from geoalchemy2.shape import to_shape
            rows = crud.get_village_history(db, village_id)
            if rows:
                history = []
                for r in rows:
                    pt = to_shape(r.point)
                    history.append({
                        "request_id": r.id,
                        "created_at": r.created_at.isoformat() if r.created_at else None,
                        "lat": pt.y,
                        "lon": pt.x,
                        "area_km2": r.terrain_result.area_km2 if r.terrain_result else None,
                        "runoff_m3": r.recommendation.runoff_m3 if r.recommendation else None,
                        "suitability_score": r.recommendation.suitability_score if r.recommendation else None,
                    })
                return {"village_id": village_id, "history": history}
        except Exception:
            pass

    # In-memory history matching village_id
    matches = [a for a in IN_MEMORY_ANALYSES if a.get("village_id") == village_id]
    return {"village_id": village_id, "history": matches}


@app.get("/api/analyses/recent")
async def get_recent_analyses_endpoint(db=Depends(_get_db_lazy)):
    if db is not None:
        try:
            from app.db import crud
            from geoalchemy2.shape import to_shape
            rows = crud.get_recent_analyses(db, limit=12)
            if rows:
                analyses = []
                for r in rows:
                    pt = to_shape(r.point)
                    analyses.append({
                        "request_id": r.id,
                        "created_at": r.created_at.isoformat() if r.created_at else None,
                        "lat": round(pt.y, 5),
                        "lon": round(pt.x, 5),
                        "area_km2": r.terrain_result.area_km2 if r.terrain_result else 0,
                        "runoff_m3": r.recommendation.runoff_m3 if r.recommendation else 0,
                        "suitability_score": r.recommendation.suitability_score if r.recommendation else 0,
                    })
                return {"analyses": analyses}
        except Exception:
            pass

    return {"analyses": IN_MEMORY_ANALYSES[:12]}


# ---------- System Health & Monitoring ----------

@app.get("/api/health", response_model=HealthResponse)
async def health_check():
    import resource
    db_status = "connected (PostgreSQL + PostGIS)" if DB_IS_AVAILABLE else "standby (resilient persistent-file mode)"
    # Current resident set size from /proc (Linux); falls back to peak RSS elsewhere.
    mem_mb = None
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    mem_mb = int(line.split()[1]) / 1024.0  # kB -> MB
                    break
    except OSError:
        pass
    if mem_mb is None:
        mem_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    return HealthResponse(
        status="healthy",
        service="Bhagiratha: AI-based Village Pond Planning System",
        database=db_status,
        uptime_seconds=round(time.time() - START_TIME, 1),
        memory_mb=round(mem_mb, 1),
        pid=os.getpid(),
    )

