# virtughan_qgis/tiler/api.py


import os
import sys
import asyncio
import importlib
import pkgutil
import inspect
import logging
import traceback
import time
import re
from collections import deque
from datetime import datetime, timedelta
from typing import Optional, Tuple

import numpy as np
from PIL import Image

from ..bootstrap import (
    RUNTIME_FALLBACK_SITE_PACKAGES_DIR,
    RUNTIME_SITE_PACKAGES_DIR,
    activate_runtime_paths,
)

try:
    psutil = importlib.import_module("psutil")  # optional, used for adaptive concurrency
except Exception:
    psutil = None

activate_runtime_paths()


def _clear_tiler_runtime_modules() -> None:
    """Clear modules that can cause class-identity mismatches across roots."""
    for mod_name in list(sys.modules.keys()):
        if (
            mod_name == "virtughan"
            or mod_name.startswith("virtughan.")
            or mod_name == "rio_tiler"
            or mod_name.startswith("rio_tiler.")
            or mod_name == "rasterio"
            or mod_name.startswith("rasterio.")
        ):
            try:
                del sys.modules[mod_name]
            except Exception:
                pass


def _in_runtime_path(path: str) -> bool:
    if not path:
        return False
    norm = os.path.normpath(path)
    runtime_candidates = [
        os.path.normpath(RUNTIME_SITE_PACKAGES_DIR),
        os.path.normpath(RUNTIME_FALLBACK_SITE_PACKAGES_DIR),
    ]
    return any(norm.startswith(candidate) for candidate in runtime_candidates if candidate)


def _module_info(name: str) -> dict:
    mod = sys.modules.get(name)
    if mod is None:
        try:
            mod = importlib.import_module(name)
        except Exception as exc:
            return {"import_error": str(exc), "loaded": False}

    mod_file = getattr(mod, "__file__", "") or ""
    return {
        "loaded": True,
        "file": mod_file,
        "in_runtime": _in_runtime_path(mod_file),
    }


# Do not purge modules at import time here; it can create class-identity
# mismatches in long-lived QGIS sessions (e.g., rasterio _ParsedPath types).


def _patch_rasterio_parsed_path_compat() -> None:
    """Make rasterio path handling robust when _ParsedPath comes from a different module instance."""
    try:
        import rasterio
    except Exception:
        return

    original_open = getattr(rasterio, "open", None)
    if original_open is None:
        return

    def _coerce_parsed_path(path_obj):
        scheme = getattr(path_obj, "scheme", None)
        path = getattr(path_obj, "path", None)
        archive = getattr(path_obj, "archive", None)
        if not path:
            return None
        if not scheme:
            return str(path)
        if archive:
            return f"{scheme}://{archive}/{str(path).lstrip('/')}"
        return f"{scheme}://{str(path).lstrip('/')}"

    def _coerce_stringified_parsed_path(fp):
        if not isinstance(fp, str):
            return fp
        s = fp.strip()
        if not (s.startswith("_ParsedPath(") and "path='" in s and "scheme='" in s):
            return fp

        try:
            # Example:
            # _ParsedPath(path='host/key.tif', archive=None, scheme='https')
            path_m = re.search(r"path='([^']*)'", s)
            scheme_m = re.search(r"scheme='([^']*)'", s)
            archive_m = re.search(r"archive='([^']*)'|archive=None", s)
            if not path_m or not scheme_m:
                return fp

            path_val = path_m.group(1)
            scheme_val = scheme_m.group(1)
            archive_val = None
            if archive_m:
                # group(1) exists only when archive='...'
                archive_val = archive_m.group(1)

            if not scheme_val:
                return path_val
            if archive_val:
                return f"{scheme_val}://{archive_val}/{path_val.lstrip('/')}"
            return f"{scheme_val}://{path_val.lstrip('/')}"
        except Exception:
            return fp

    def _coerce_if_foreign_parsed_path(fp):
        fp = _coerce_stringified_parsed_path(fp)
        # Handle foreign _ParsedPath instances or ParsedPath-like objects from mixed module identities.
        if fp is not None and (
            fp.__class__.__name__ == "_ParsedPath"
            or (
                hasattr(fp, "path")
                and hasattr(fp, "scheme")
                and hasattr(fp, "archive")
                and not isinstance(fp, (str, bytes, os.PathLike))
            )
        ):
            rebuilt = _coerce_parsed_path(fp)
            if rebuilt:
                return rebuilt
        return fp

    def _patched_open(fp, *args, **kwargs):
        fp = _coerce_if_foreign_parsed_path(fp)
        return original_open(fp, *args, **kwargs)

    if not getattr(original_open, "_virtughan_patched", False):
        _patched_open._virtughan_patched = True
        rasterio.open = _patched_open

    # Some call paths fail before rasterio.open is reached, inside _parse_path.
    # Patch all known rasterio parse modules to avoid mixed-identity gaps.
    parse_mod_names = {"rasterio._path", "rasterio.path"}
    parse_mod_names.update(
        name for name in list(sys.modules.keys()) if name.startswith("rasterio") and name.endswith("path")
    )

    for mod_name in parse_mod_names:
        try:
            path_mod = importlib.import_module(mod_name)
        except Exception:
            continue

        original_parse_path = getattr(path_mod, "_parse_path", None)
        if not original_parse_path or getattr(original_parse_path, "_virtughan_patched", False):
            continue

        def _patched_parse_path(fp, *args, __orig=original_parse_path, **kwargs):
            fp = _coerce_if_foreign_parsed_path(fp)
            return __orig(fp, *args, **kwargs)

        _patched_parse_path._virtughan_patched = True
        path_mod._parse_path = _patched_parse_path


_patch_rasterio_parsed_path_compat()

import matplotlib
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

matplotlib.use("Agg")


def _find_tileprocessor() -> Tuple[type, str]:
    """
    Locate a class named 'TileProcessor' somewhere under virtughan.*.
    Returns (class, 'module:Class') or raises ImportError with a clear message.
    """
    try:
        import virtughan  
    except Exception as e:
        raise ImportError(
            "Cannot import 'virtughan' in this QGIS Python. "
            "Install it into the same interpreter QGIS uses. "
            f"Underlying error: {e}"
        )

    mod_file = os.path.normpath(getattr(virtughan, "__file__", "") or "")
    if mod_file and not _in_runtime_path(mod_file):
        raise ImportError(
            "Imported 'virtughan' from non-runtime path: "
            f"{mod_file}. Expected under runtime folders."
        )

    import virtughan  
    for m in pkgutil.walk_packages(virtughan.__path__, "virtughan."):
        if m.ispkg:
            continue
        try:
            mod = importlib.import_module(m.name)
        except Exception:
            continue
        for name, obj in vars(mod).items():
            if inspect.isclass(obj) and name == "TileProcessor":
                return obj, f"{m.name}:{name}"

    
    try:
        from virtughan.tile import TileProcessor  
        return TileProcessor, "virtughan.tile:TileProcessor"
    except Exception:
        pass

    raise ImportError("TileProcessor not found anywhere under virtughan.*.")


TileProcessor, TP_path = _find_tileprocessor()


def _safe_apply_colormap(result, colormap_str):
    """Apply a simple red-yellow-green ramp without relying on matplotlib internals."""
    arr = np.asarray(result, dtype=float)
    finite_mask = np.isfinite(arr)
    if not finite_mask.any():
        return Image.fromarray(np.zeros((arr.shape[0], arr.shape[1], 3), dtype=np.uint8))

    valid = arr[finite_mask]
    vmin = float(valid.min())
    vmax = float(valid.max())
    if vmax <= vmin:
        norm = np.zeros_like(arr, dtype=float)
    else:
        norm = (arr - vmin) / (vmax - vmin)
    norm = np.clip(norm, 0.0, 1.0)

    # RdYlGn-like ramp: red -> yellow -> green.
    r = np.where(norm < 0.5, 1.0, 2.0 - 2.0 * norm)
    g = np.where(norm < 0.5, 2.0 * norm, 1.0)
    b = np.zeros_like(norm)

    rgb = np.stack([r, g, b], axis=-1)
    rgb[~finite_mask] = 0.0
    rgb_u8 = (rgb * 255).astype(np.uint8)
    return Image.fromarray(rgb_u8)


TileProcessor.apply_colormap = staticmethod(_safe_apply_colormap)


def _default_tiler_concurrency() -> int:
    # Use one consistent default across platforms.
    return 4


def _resolve_tiler_concurrency() -> int:
    raw = os.getenv("VIRTUGHAN_TILER_CONCURRENCY", "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except Exception:
            pass
    return _default_tiler_concurrency()


app = FastAPI(title="virtughan tiler (QGIS local)")
processor = TileProcessor(cache_time=60)  
logger = logging.getLogger(__name__)
_TILER_CONCURRENCY = _resolve_tiler_concurrency()
_TILE_REQUEST_SEMAPHORES: dict[int, asyncio.Semaphore] = {}
_QUEUE_WAIT_TIMEOUT_SEC = 12.0
_TILE_COMPUTE_TIMEOUT_SEC = 16.0
_LAST_TILE_ERROR: dict = {}
_VIEW_GENERATION = 0
_ACTIVE_TILE_REQUESTS = 0
_TILER_LOGS = deque(maxlen=500)
_TILER_LOG_SEQ = 0
_URL_LOG_SAMPLE_LIMIT = 5
_URL_LOGGED_COUNT = 0
_STALE_CHECK_SLICE_SEC = 0.05
_VIEW_SETTLE_DELAY_SEC = max(0.0, float(os.getenv("VIRTUGHAN_VIEW_SETTLE_DELAY_SEC", "0.35") or 0.35))
_LAST_VIEW_CHANGE_MONOTONIC = 0.0
_CPU_COUNT = max(1, int(os.cpu_count() or 1))
_MAX_DYNAMIC_CONCURRENCY = 4
_MIN_DYNAMIC_CONCURRENCY = 4


def _cpu_usage_percent() -> Optional[float]:
    if psutil is None:
        return None
    try:
        return float(psutil.cpu_percent(interval=None))
    except Exception:
        return None


def _dynamic_concurrency_limit() -> int:
    """Keep worker concurrency fixed for stability."""
    return 4


def _get_request_semaphore() -> asyncio.Semaphore:
    """Return a semaphore bound to the currently running event loop."""
    loop = asyncio.get_running_loop()
    key = id(loop)
    sem = _TILE_REQUEST_SEMAPHORES.get(key)
    if sem is None:
        sem = asyncio.Semaphore(max(_TILER_CONCURRENCY, _MAX_DYNAMIC_CONCURRENCY))
        _TILE_REQUEST_SEMAPHORES[key] = sem
    return sem


def _append_tiler_log(level: str, message: str, **extra):
    global _TILER_LOG_SEQ
    _TILER_LOG_SEQ += 1
    entry = {
        "id": _TILER_LOG_SEQ,
        "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "level": level,
        "message": message,
    }
    if extra:
        entry.update(extra)
    _TILER_LOGS.append(entry)


def _append_tile_event_log(level: str, message: str, tile: str, **extra):
    """Log standard tile events without URL noise."""
    payload = {"tile": tile}
    if extra:
        payload.update(extra)

    _append_tiler_log(level, message, **payload)


def _append_tile_url_sample(tile: str, full_url: str):
    """Log only a small sample of full tile request URLs for debugging."""
    global _URL_LOGGED_COUNT
    if not full_url or _URL_LOGGED_COUNT >= _URL_LOG_SAMPLE_LIMIT:
        return

    _URL_LOGGED_COUNT += 1
    payload = {"tile": tile, "url": full_url}
    if _URL_LOGGED_COUNT == _URL_LOG_SAMPLE_LIMIT:
        payload["note"] = "URL sample limit reached; suppressing further tile URLs"

    _append_tiler_log("info", "Sample tile request URL", **payload)


def _retryable_tile_unavailable(reason: str, status_code: int = 503) -> Response:
    """Return a non-cacheable response so clients can re-request missing tiles."""
    return Response(
        content=reason,
        media_type="text/plain",
        status_code=status_code,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
            "Retry-After": "2",
            "X-Tile-Retryable": "1",
            "X-Tile-Reason": reason,
        },
    )


def _record_tile_failure(reason: str, status: int, req_tag: str, req_url: str, **extra) -> None:
    """Record a tile failure in diagnostics with full URL + response metadata."""
    payload = {
        "reason": reason,
        "status": int(status),
        "tile": req_tag,
        "url": req_url,
    }
    if extra:
        payload.update(extra)

    _LAST_TILE_ERROR.clear()
    _LAST_TILE_ERROR.update(payload)
    _append_tiler_log("warning", "Tile response failed", **payload)


def _is_stale_generation(request_generation: int) -> bool:
    return int(request_generation) != int(_VIEW_GENERATION)


def _is_view_settling() -> bool:
    if _VIEW_SETTLE_DELAY_SEC <= 0.0:
        return False
    if _LAST_VIEW_CHANGE_MONOTONIC <= 0.0:
        return False
    return (time.monotonic() - _LAST_VIEW_CHANGE_MONOTONIC) < _VIEW_SETTLE_DELAY_SEC


def _view_settle_remaining_sec() -> float:
    if _VIEW_SETTLE_DELAY_SEC <= 0.0 or _LAST_VIEW_CHANGE_MONOTONIC <= 0.0:
        return 0.0
    elapsed = time.monotonic() - _LAST_VIEW_CHANGE_MONOTONIC
    return max(0.0, _VIEW_SETTLE_DELAY_SEC - elapsed)


def _stale_tile_response(reason: str, req_tag: str, req_url: str) -> Response:
    _record_tile_failure(
        reason=reason,
        status=409,
        req_tag=req_tag,
        req_url=req_url,
        response=reason,
    )
    return _retryable_tile_unavailable(reason, status_code=409)


def _tile_tag_from_path(path: str) -> str:
    try:
        prefix = "/tile/"
        if not path or not path.startswith(prefix):
            return ""
        rest = path[len(prefix):].strip("/")
        parts = rest.split("/")
        if len(parts) >= 3:
            return f"{parts[0]}/{parts[1]}/{parts[2]}"
    except Exception:
        pass
    return ""


@app.exception_handler(Exception)
async def _handle_unhandled_exception(request: Request, exc: Exception):
    req_url = str(request.url)
    req_path = request.url.path
    req_tag = _tile_tag_from_path(req_path)

    if req_tag:
        _record_tile_failure(
            reason="unhandled_exception",
            status=500,
            req_tag=req_tag,
            req_url=req_url,
            response=str(exc),
            error=exc.__class__.__name__,
        )
        _append_tiler_log(
            "error",
            "Unhandled tile exception",
            tile=req_tag,
            url=req_url,
            status=500,
            response=str(exc),
            error=exc.__class__.__name__,
            detail=str(exc),
        )
    else:
        _append_tiler_log(
            "error",
            "Unhandled server exception",
            url=req_url,
            status=500,
            response=str(exc),
            error=exc.__class__.__name__,
            detail=str(exc),
        )

    return JSONResponse(
        status_code=500,
        content={
            "error": "unhandled_exception",
            "exception_type": exc.__class__.__name__,
            "detail": str(exc),
            "url": req_url,
        },
    )


async def _generate_tile_with_cache_fallback(**kwargs):
    """Generate tile cache-first, with uncached fallback only for recursion issues."""
    raw_func = getattr(processor.cached_generate_tile, "__wrapped__", None)
    try:
        return await processor.cached_generate_tile(**kwargs)
    except Exception as exc:
        if raw_func is None:
            raise
        logger.warning(
            "Cached path failed (%s); retrying uncached tile generation",
            exc.__class__.__name__,
        )
        return await raw_func(processor, **kwargs)


@app.get("/health")
async def health():
    return {"status": "ok", "python": sys.executable}


@app.get("/whoami")
async def whoami():
    return {
        "tileprocessor": TP_path,
        "processor_type": f"{processor.__class__.__module__}.{processor.__class__.__name__}",
        "cwd": os.getcwd(),
    }


@app.get("/diag/imports")
async def diag_imports():
    cpu_usage = _cpu_usage_percent()
    return {
        "runtime_site_packages": RUNTIME_SITE_PACKAGES_DIR,
        "runtime_fallback_site_packages": RUNTIME_FALLBACK_SITE_PACKAGES_DIR,
        "request_concurrency": _TILER_CONCURRENCY,
        "dynamic_concurrency": {
            "active_limit": _dynamic_concurrency_limit(),
            "min": _MIN_DYNAMIC_CONCURRENCY,
            "max": _MAX_DYNAMIC_CONCURRENCY,
            "cpu_usage_percent": cpu_usage,
            "psutil_available": bool(psutil is not None),
        },
        "modules": {
            "virtughan": _module_info("virtughan"),
            "rio_tiler": _module_info("rio_tiler"),
            "rasterio": _module_info("rasterio"),
            "rasterio._path": _module_info("rasterio._path"),
            "numpy": _module_info("numpy"),
            "matplotlib": _module_info("matplotlib"),
            "aiocache": _module_info("aiocache"),
        },
    }


@app.get("/diag/last-error")
async def diag_last_error():
    return _LAST_TILE_ERROR or {"status": "no tile errors recorded"}


@app.post("/diag/bump-generation")
@app.get("/diag/bump-generation")
async def bump_generation(reason: str = Query("view_changed")):
    global _VIEW_GENERATION, _LAST_VIEW_CHANGE_MONOTONIC
    _VIEW_GENERATION += 1
    _LAST_VIEW_CHANGE_MONOTONIC = time.monotonic()
    _append_tiler_log("info", "View generation bumped", reason=reason, generation=_VIEW_GENERATION)
    return {"generation": _VIEW_GENERATION, "reason": reason}


@app.get("/diag/logs")
async def diag_logs(since_id: int = Query(0)):
    rows = [row for row in _TILER_LOGS if row.get("id", 0) > int(since_id)]
    return {
        "rows": rows,
        "last_id": _TILER_LOG_SEQ,
        "generation": _VIEW_GENERATION,
        "active_requests": _ACTIVE_TILE_REQUESTS,
        "request_concurrency": _TILER_CONCURRENCY,
    }


@app.get("/diag/runtime")
async def diag_runtime():
    cpu_usage = _cpu_usage_percent()
    return {
        "active_requests": _ACTIVE_TILE_REQUESTS,
        "base_request_concurrency": _TILER_CONCURRENCY,
        "dynamic": {
            "active_limit": _dynamic_concurrency_limit(),
            "min": _MIN_DYNAMIC_CONCURRENCY,
            "max": _MAX_DYNAMIC_CONCURRENCY,
            "cpu_usage_percent": cpu_usage,
            "psutil_available": bool(psutil is not None),
        },
        "view_settle_delay_sec": _VIEW_SETTLE_DELAY_SEC,
    }


@app.get("/tile/{z}/{x}/{y}")
async def get_tile(
    request: Request,
    z: int,
    x: int,
    y: int,
    start_date: str = Query(None),
    end_date: str = Query(None),
    cloud_cover: int = Query(30),
    band1: str = Query("visual", description="visual, red, green, blue, nir, swir1, swir2"),
    band2: Optional[str] = Query(None),
    formula: str = Query("band1", description="(band2 - band1)/(band2 + band1) or 'band1' for visual"),
    colormap_str: str = Query("RdYlGn"),
    operation: str = Query("median"),
    timeseries: bool = Query(False),
):
    # Keep compatibility patches current in long-lived QGIS sessions.
    _patch_rasterio_parsed_path_compat()

    
    if z < 10 or z > 23:
        return JSONResponse(content={"error": "Zoom level must be between 10 and 23"}, status_code=400)

    
    if not start_date:
        start_date = (datetime.utcnow() - timedelta(days=360)).strftime("%Y-%m-%d")
    if not end_date:
        end_date = datetime.utcnow().strftime("%Y-%m-%d")

    req_tag = f"{z}/{x}/{y}"
    request_generation = int(_VIEW_GENERATION)
    req_url = str(request.url)
    _append_tile_url_sample(tile=req_tag, full_url=req_url)
    global _ACTIVE_TILE_REQUESTS
    sem = _get_request_semaphore()

    acquired = False
    counted_active = False
    try:
        if _is_view_settling():
            wait_sec = min(0.6, _view_settle_remaining_sec())
            if wait_sec > 0.0:
                _append_tile_event_log("info", "Waiting for view settle", tile=req_tag, wait_sec=round(wait_sec, 3))
                await asyncio.sleep(wait_sec)

        current_limit = _dynamic_concurrency_limit()
        _append_tile_event_log(
            "info",
            "Tile queued; waiting for worker",
            tile=req_tag,
            limit=current_limit,
        )
        waited = 0.0
        while waited < _QUEUE_WAIT_TIMEOUT_SEC and not acquired:
            if _is_stale_generation(request_generation):
                _append_tile_event_log("info", "Dropped stale queued tile", tile=req_tag)
                return _stale_tile_response("stale_view_generation", req_tag=req_tag, req_url=req_url)
            slice_sec = min(_STALE_CHECK_SLICE_SEC, _QUEUE_WAIT_TIMEOUT_SEC - waited)
            try:
                await asyncio.wait_for(sem.acquire(), timeout=slice_sec)
                acquired = True
                break
            except asyncio.TimeoutError:
                waited += slice_sec

        if not acquired:
            _append_tile_event_log("warning", "Dropped tile after queue wait timeout", tile=req_tag)
            _record_tile_failure(
                reason="queue_wait_timeout",
                status=503,
                req_tag=req_tag,
                req_url=req_url,
                response="queue_wait_timeout",
            )
            return _retryable_tile_unavailable("queue_wait_timeout", status_code=503)

        _ACTIVE_TILE_REQUESTS += 1
        counted_active = True

        if _is_stale_generation(request_generation):
            _append_tile_event_log("info", "Dropped stale tile before compute", tile=req_tag)
            return _stale_tile_response("stale_view_generation", req_tag=req_tag, req_url=req_url)

        image_bytes, feature = await asyncio.wait_for(
            _generate_tile_with_cache_fallback(
                x=x,
                y=y,
                z=z,
                start_date=start_date,
                end_date=end_date,
                cloud_cover=cloud_cover,
                band1=band1,
                band2=(band2 or ""),
                formula=formula,
                colormap_str=colormap_str,
                operation=operation,
                latest=not timeseries,
            ),
            timeout=_TILE_COMPUTE_TIMEOUT_SEC,
        )
        headers = {}
        try:
            props = feature.get("properties", {})
            if "datetime" in props:
                headers["X-Image-Date"] = props["datetime"]
            if "eo:cloud_cover" in props:
                headers["X-Cloud-Cover"] = str(props["eo:cloud_cover"])
        except Exception:
            pass
        headers["Cache-Control"] = "public, max-age=300"
        _LAST_TILE_ERROR.clear()
        _append_tile_event_log("info", "Served tile", tile=req_tag)
        return Response(content=image_bytes, media_type="image/png", headers=headers)

    except HTTPException as he:
        return JSONResponse(status_code=he.status_code, content={"detail": he.detail})
    except asyncio.TimeoutError:
        _append_tile_event_log("warning", "Tile generation timeout", tile=req_tag)
        _record_tile_failure(
            reason="tile_compute_timeout",
            status=504,
            req_tag=req_tag,
            req_url=req_url,
            response="tile_compute_timeout",
        )
        return _retryable_tile_unavailable("tile_compute_timeout", status_code=504)
    except asyncio.CancelledError:
        _append_tile_event_log("warning", "Tile generation cancelled", tile=req_tag)
        _record_tile_failure(
            reason="tile_compute_cancelled",
            status=503,
            req_tag=req_tag,
            req_url=req_url,
            response="tile_compute_cancelled",
        )
        return _retryable_tile_unavailable("tile_compute_cancelled", status_code=503)
    except Exception as ex:
        tb = traceback.format_exc()
        err_payload = {
            "error": f"Computation Error: {str(ex)}",
            "exception_type": ex.__class__.__name__,
            "traceback": tb,
            "request": {
                "z": z,
                "x": x,
                "y": y,
                "start_date": start_date,
                "end_date": end_date,
                "cloud_cover": cloud_cover,
                "band1": band1,
                "band2": band2,
                "formula": formula,
                "colormap_str": colormap_str,
                "operation": operation,
                "timeseries": timeseries,
            },
        }
        _LAST_TILE_ERROR.clear()
        _LAST_TILE_ERROR.update(err_payload)
        _append_tiler_log(
            "error",
            "Tile request failed response",
            tile=req_tag,
            url=req_url,
            status=500,
            response=str(ex),
            error=ex.__class__.__name__,
            detail=str(ex),
        )
        _append_tile_event_log(
            "error",
            "Tile request failed",
            tile=req_tag,
            error=ex.__class__.__name__,
            detail=str(ex),
        )
        logger.exception("Tile request failed", exc_info=ex)
        return JSONResponse(content=err_payload, status_code=500)
    finally:
        if acquired:
            try:
                sem.release()
            except Exception:
                pass
        if counted_active and _ACTIVE_TILE_REQUESTS > 0:
            _ACTIVE_TILE_REQUESTS -= 1
