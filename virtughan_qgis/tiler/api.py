# virtughan_qgis/tiler/api.py


import os
import sys
import importlib
import pkgutil
import inspect
from datetime import datetime, timedelta
from typing import Optional, Tuple

from ..bootstrap import RUNTIME_SITE_PACKAGES_DIR, activate_runtime_paths

activate_runtime_paths()

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
    runtime_site = os.path.normpath(RUNTIME_SITE_PACKAGES_DIR)
    if mod_file and runtime_site and not mod_file.startswith(runtime_site):
        raise ImportError(
            "Imported 'virtughan' from non-runtime path: "
            f"{mod_file}. Expected under: {RUNTIME_SITE_PACKAGES_DIR}"
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

app = FastAPI(title="virtughan tiler (QGIS local)")
processor = TileProcessor(cache_time=60)  


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
        
        image_bytes, feature = await processor.cached_generate_tile(
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
        return Response(content=image_bytes, media_type="image/png", headers=headers)

    except HTTPException as he:
        return JSONResponse(status_code=he.status_code, content={"detail": he.detail})
    except Exception as ex:
        return JSONResponse(content={"error": f"Computation Error: {str(ex)}"}, status_code=500)
