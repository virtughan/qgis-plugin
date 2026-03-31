# virtughan_qgis/tiler/api.py


import os
import sys
import asyncio
import importlib
import pkgutil
import inspect
import logging
import traceback
from datetime import datetime, timedelta
from typing import Optional, Tuple

import numpy as np
from PIL import Image

from ..bootstrap import (
    RUNTIME_FALLBACK_SITE_PACKAGES_DIR,
    RUNTIME_SITE_PACKAGES_DIR,
    activate_runtime_paths,
)

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
    """Make rasterio.open robust when _ParsedPath comes from a different module instance."""
    try:
        import rasterio
    except Exception:
        return

    original_open = getattr(rasterio, "open", None)
    if original_open is None:
        return
    if getattr(original_open, "_virtughan_patched", False):
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

    def _patched_open(fp, *args, **kwargs):
        if fp is not None and fp.__class__.__name__ == "_ParsedPath":
            rebuilt = _coerce_parsed_path(fp)
            if rebuilt:
                fp = rebuilt
        return original_open(fp, *args, **kwargs)

    _patched_open._virtughan_patched = True
    rasterio.open = _patched_open


_patch_rasterio_parsed_path_compat()

import matplotlib
from fastapi import FastAPI, HTTPException, Query, Response
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

app = FastAPI(title="virtughan tiler (QGIS local)")
processor = TileProcessor(cache_time=60)  
logger = logging.getLogger(__name__)
_TILE_REQUEST_SEMAPHORE = asyncio.Semaphore(4)
_LAST_TILE_ERROR: dict = {}


async def _generate_tile_with_cache_fallback(**kwargs):
    """Generate tile cache-first, with uncached fallback only for recursion issues."""
    async with _TILE_REQUEST_SEMAPHORE:
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
    return {
        "runtime_site_packages": RUNTIME_SITE_PACKAGES_DIR,
        "runtime_fallback_site_packages": RUNTIME_FALLBACK_SITE_PACKAGES_DIR,
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


@app.get("/tile/{z}/{x}/{y}")
async def get_tile(
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
    
    if z < 10 or z > 23:
        return JSONResponse(content={"error": "Zoom level must be between 10 and 23"}, status_code=400)

    
    if not start_date:
        start_date = (datetime.utcnow() - timedelta(days=360)).strftime("%Y-%m-%d")
    if not end_date:
        end_date = datetime.utcnow().strftime("%Y-%m-%d")

    try:
        
        image_bytes, feature = await _generate_tile_with_cache_fallback(
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
        _LAST_TILE_ERROR.clear()
        return Response(content=image_bytes, media_type="image/png", headers=headers)

    except HTTPException as he:
        return JSONResponse(status_code=he.status_code, content={"detail": he.detail})
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
        logger.exception("Tile request failed", exc_info=ex)
        return JSONResponse(content=err_payload, status_code=500)
