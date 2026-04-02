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
import math
import json
import tempfile
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


def _prefer_primary_runtime_paths() -> None:
    """Keep primary runtime first in sys.path and fallback paths last."""
    primary_site = os.path.normpath(RUNTIME_SITE_PACKAGES_DIR)
    primary_root = os.path.normpath(os.path.dirname(RUNTIME_SITE_PACKAGES_DIR))
    fallback_site = os.path.normpath(RUNTIME_FALLBACK_SITE_PACKAGES_DIR)
    fallback_root = os.path.normpath(os.path.dirname(RUNTIME_FALLBACK_SITE_PACKAGES_DIR))

    ordered = [primary_site, primary_root, fallback_root, fallback_site]
    for p in ordered:
        try:
            while p in sys.path:
                sys.path.remove(p)
        except Exception:
            pass

    # Primary first, fallback retained only as last resort.
    sys.path.insert(0, primary_root)
    sys.path.insert(0, primary_site)
    sys.path.append(fallback_root)
    sys.path.append(fallback_site)


_prefer_primary_runtime_paths()


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
        # Handle wrapped string repr forms like "'_ParsedPath(...)'" or "\"ParsedPath(...)\"".
        if (len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"')):
            s = s[1:-1].strip()

        # Some call paths pass wrappers like:
        # "invalid path '_ParsedPath(path=..., scheme=...)'"
        # Extract the ParsedPath(...) payload if embedded in other text.
        candidate = s
        if not (
            (candidate.startswith("_ParsedPath(") or candidate.startswith("ParsedPath("))
            and "path=" in candidate
            and "scheme=" in candidate
        ):
            embedded = re.search(r"(_?ParsedPath\([^\)]*\))", candidate)
            if embedded:
                candidate = embedded.group(1)
            else:
                return fp

        try:
            # Examples:
            # _ParsedPath(path='host/key.tif', archive=None, scheme='https')
            # ParsedPath(path="host/key.tif", archive=None, scheme="https")
            path_m = re.search(r"path=(?:'([^']*)'|\"([^\"]*)\")", candidate)
            scheme_m = re.search(r"scheme=(?:'([^']*)'|\"([^\"]*)\")", candidate)
            archive_m = re.search(r"archive=(?:'([^']*)'|\"([^\"]*)\"|None)", candidate)
            if not path_m or not scheme_m:
                return fp

            path_val = path_m.group(1) or path_m.group(2) or ""
            scheme_val = scheme_m.group(1) or scheme_m.group(2) or ""
            archive_val = None
            if archive_m:
                archive_val = archive_m.group(1) or archive_m.group(2)

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

    def _wrap_parse_callable(parse_func):
        if not parse_func or getattr(parse_func, "_virtughan_patched", False):
            return parse_func

        def _patched_parse(fp, *args, __orig=parse_func, **kwargs):
            fp = _coerce_if_foreign_parsed_path(fp)
            return __orig(fp, *args, **kwargs)

        _patched_parse._virtughan_patched = True
        return _patched_parse

    # Some call paths fail before rasterio.open is reached, inside parse_path/_parse_path.
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

        if hasattr(path_mod, "_parse_path"):
            patched = _wrap_parse_callable(getattr(path_mod, "_parse_path", None))
            if patched is not None:
                path_mod._parse_path = patched
        if hasattr(path_mod, "parse_path"):
            patched = _wrap_parse_callable(getattr(path_mod, "parse_path", None))
            if patched is not None:
                path_mod.parse_path = patched

    # Any already-loaded module can import parse_path/_parse_path/open into module scope.
    # Rebind aliases broadly so stale function references do not bypass this compatibility layer.
    for mod_name, mod in list(sys.modules.items()):
        if mod is None:
            continue
        try:
            for attr_name in ("parse_path", "_parse_path"):
                if hasattr(mod, attr_name):
                    patched = _wrap_parse_callable(getattr(mod, attr_name, None))
                    if patched is not None:
                        setattr(mod, attr_name, patched)

            # Some modules keep a direct alias to rasterio.open.
            open_attr = getattr(mod, "open", None)
            if callable(open_attr) and getattr(open_attr, "__module__", "").startswith("rasterio"):
                setattr(mod, "open", rasterio.open)
        except Exception:
            continue


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
    # rio-tiler can return masked/object arrays containing sentinel values
    # (e.g., numpy.ma._NoValueType). Normalize to plain float first.
    raw = result
    if np.ma.isMaskedArray(raw):
        raw = np.ma.filled(raw, np.nan)

    arr = np.asarray(raw)
    if arr.dtype == object:
        def _safe_float(v):
            if v is None:
                return np.nan
            cls_name = getattr(v, "__class__", type(v)).__name__
            if cls_name in {"_NoValueType", "MaskedConstant"}:
                return np.nan
            try:
                return float(v)
            except Exception:
                return np.nan

        arr = np.vectorize(_safe_float, otypes=[float])(arr)
    else:
        arr = arr.astype(float, copy=False)

    finite_mask = np.isfinite(arr)
    if not finite_mask.any():
        return Image.fromarray(np.zeros((arr.shape[0], arr.shape[1], 3), dtype=np.uint8))

    valid = arr[finite_mask]
    vmin = float(np.nanmin(valid))
    vmax = float(np.nanmax(valid))
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


def _patch_tileprocessor_fetch_tile_compat() -> None:
    """Normalize fetched tile arrays so downstream math never sees _NoValue sentinels."""
    current_fetch = getattr(TileProcessor, "fetch_tile", None)
    if current_fetch is None:
        return

    # Always patch using the original fetch function, even if an older compat wrapper exists.
    original_fetch = getattr(current_fetch, "_virtughan_original_fetch", current_fetch)

    def _coerce_no_value_safe_float_array(value):
        if np.ma.isMaskedArray(value):
            return np.asarray(np.ma.filled(value, np.nan), dtype=float)

        # rio-tiler can return ImageData-like objects in some versions.
        if hasattr(value, "data") and not isinstance(value, np.ndarray):
            value = getattr(value, "data")

        # Some paths can return tuples/lists with the array in the first slot.
        if isinstance(value, (tuple, list)) and value:
            value = value[0]

        arr = np.asarray(value)
        if arr.dtype != object:
            return arr.astype(float, copy=False)

        def _safe_float(v):
            if v is None:
                return np.nan
            cls_name = getattr(v, "__class__", type(v)).__name__
            if cls_name in {"_NoValueType", "MaskedConstant"}:
                return np.nan
            try:
                return float(v)
            except Exception:
                return np.nan

        return np.vectorize(_safe_float, otypes=[float])(arr)

    async def _patched_fetch_tile(url, x, y, z):
        tile = await original_fetch(url, x, y, z)
        try:
            return _coerce_no_value_safe_float_array(tile)
        except Exception:
            # Fallback to original tile payload if coercion is not possible.
            return tile

    _patched_fetch_tile._virtughan_patched = True
    _patched_fetch_tile._virtughan_original_fetch = original_fetch
    TileProcessor.fetch_tile = staticmethod(_patched_fetch_tile)


TileProcessor.apply_colormap = staticmethod(_safe_apply_colormap)
_patch_tileprocessor_fetch_tile_compat()


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


@app.on_event("startup")
async def _on_startup_log_fingerprint():
    _ensure_consistent_runtime_roots()
    _stabilize_startup_path_bindings()
    _persist_and_log_startup_fingerprint()


_TILER_CONCURRENCY = _resolve_tiler_concurrency()
_TILE_REQUEST_SEMAPHORES: dict[tuple[int, int], asyncio.Semaphore] = {}
_QUEUE_WAIT_TIMEOUT_SEC = 12.0
_TILE_COMPUTE_TIMEOUT_SEC = 16.0
_LAST_TILE_ERROR: dict = {}
_VIEW_GENERATION = 0
_SEMAPHORE_EPOCH = 0
_ACTIVE_TILE_REQUESTS = 0
_TILER_LOGS = deque(maxlen=500)
_TILER_LOG_SEQ = 0
_URL_LOG_SAMPLE_LIMIT = 5
_URL_LOGGED_COUNT = 0
_STALE_CHECK_SLICE_SEC = 0.05
_DEFAULT_VIEW_SETTLE_DELAY_SEC = max(0.0, float(os.getenv("VIRTUGHAN_VIEW_SETTLE_DELAY_SEC", "1.5") or 1.5))
_VIEW_SETTLE_DELAY_SEC = _DEFAULT_VIEW_SETTLE_DELAY_SEC
_LAST_VIEW_CHANGE_MONOTONIC = 0.0
_GENERATION_REQUEST_TASKS: dict[int, set[asyncio.Task]] = {}
_SETTLED_VIEWPORT: dict = {}
_MAX_DYNAMIC_CONCURRENCY = 4
_MIN_DYNAMIC_CONCURRENCY = 4
_PARSEDPATH_RECOVERY_IN_PROGRESS = False
_LAST_PARSEDPATH_RECOVERY_MONOTONIC = 0.0
_PARSEDPATH_RECOVERY_COOLDOWN_SEC = 2.0
_STARTUP_STATE_FILE = os.path.join(tempfile.gettempdir(), "virtughan_tiler_startup_state.json")


def _module_file(name: str) -> str:
    try:
        mod = importlib.import_module(name)
        return str(getattr(mod, "__file__", "") or "")
    except Exception:
        return ""


def _startup_fingerprint() -> dict:
    parse_targets = {}
    for mod_name in ("rasterio._path",):
        try:
            mod = importlib.import_module(mod_name)
            parse_targets[mod_name] = {
                "file": str(getattr(mod, "__file__", "") or ""),
                "parse_path_id": id(getattr(mod, "parse_path", None)),
                "_parse_path_id": id(getattr(mod, "_parse_path", None)),
            }
        except Exception:
            parse_targets[mod_name] = {"file": "", "parse_path_id": 0, "_parse_path_id": 0}

    return {
        "python": sys.executable,
        "pid": os.getpid(),
        "cwd": os.getcwd(),
        "virtughan_file": _module_file("virtughan"),
        "rasterio_file": _module_file("rasterio"),
        "rio_tiler_file": _module_file("rio_tiler"),
        "parse_targets": parse_targets,
    }


def _runtime_state_snapshot() -> dict:
    snap = {
        "pid": os.getpid(),
        "cwd": os.getcwd(),
        "python": sys.executable,
        "virtughan_file": _module_file("virtughan"),
        "rasterio_file": _module_file("rasterio"),
        "rio_tiler_file": _module_file("rio_tiler"),
    }
    try:
        rp = importlib.import_module("rasterio._path")
        snap["rasterio_path_file"] = str(getattr(rp, "__file__", "") or "")
        snap["parse_path_id"] = id(getattr(rp, "parse_path", None))
        snap["_parse_path_id"] = id(getattr(rp, "_parse_path", None))
        snap["parsed_path_class_id"] = id(getattr(rp, "_ParsedPath", None))
    except Exception as exc:
        snap["rasterio_path_error"] = str(exc)

    try:
        import virtughan.tile as vt
        snap["tileprocessor_class_id"] = id(getattr(vt, "TileProcessor", None))
        snap["tileprocessor_fetch_id"] = id(getattr(getattr(vt, "TileProcessor", object), "fetch_tile", None))
    except Exception as exc:
        snap["tileprocessor_error"] = str(exc)

    try:
        roots = [p for p in sys.path if "virtughan_runtime" in (p or "")]
        snap["runtime_paths"] = roots[:8]
    except Exception:
        pass
    return snap


def _persist_and_log_startup_fingerprint() -> None:
    current = _startup_fingerprint()
    previous = None
    try:
        if os.path.exists(_STARTUP_STATE_FILE):
            with open(_STARTUP_STATE_FILE, "r", encoding="utf-8") as fh:
                previous = json.load(fh)
    except Exception:
        previous = None

    _append_tiler_log(
        "info",
        "Startup fingerprint",
        python=current.get("python", ""),
        pid=current.get("pid", 0),
        cwd=current.get("cwd", ""),
        virtughan=current.get("virtughan_file", ""),
        rasterio=current.get("rasterio_file", ""),
        rio_tiler=current.get("rio_tiler_file", ""),
        parse_path_id=current.get("parse_targets", {}).get("rasterio._path", {}).get("parse_path_id", 0),
        _parse_path_id=current.get("parse_targets", {}).get("rasterio._path", {}).get("_parse_path_id", 0),
    )
    _append_tiler_log("info", "Startup runtime state", detail=json.dumps(_runtime_state_snapshot(), default=str))

    if isinstance(previous, dict):
        changed = []
        for key in ("python", "virtughan_file", "rasterio_file", "rio_tiler_file"):
            if str(previous.get(key, "")) != str(current.get(key, "")):
                changed.append(key)
        if changed:
            _append_tiler_log("warning", "Restart fingerprint changed", fields=",".join(changed))
        else:
            _append_tiler_log("info", "Restart fingerprint unchanged")

    try:
        with open(_STARTUP_STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(current, fh)
    except Exception:
        pass


def _runtime_site_label(module_file: str) -> str:
    norm = os.path.normpath(module_file or "")
    primary = os.path.normpath(RUNTIME_SITE_PACKAGES_DIR)
    fallback = os.path.normpath(RUNTIME_FALLBACK_SITE_PACKAGES_DIR)
    if norm.startswith(primary):
        return "primary"
    if norm.startswith(fallback):
        return "fallback"
    return "other"


def _ensure_consistent_runtime_roots() -> None:
    """Ensure virtughan/rasterio/rio_tiler come from one runtime root on startup."""
    global TileProcessor, TP_path, processor

    current = {
        "virtughan": _module_file("virtughan"),
        "rasterio": _module_file("rasterio"),
        "rio_tiler": _module_file("rio_tiler"),
    }
    labels = {name: _runtime_site_label(path) for name, path in current.items()}
    runtime_labels = [lab for lab in labels.values() if lab in ("primary", "fallback")]

    # Nothing to do when already aligned or modules are not runtime-backed.
    if not runtime_labels or len(set(runtime_labels)) <= 1:
        return

    # Always converge to primary runtime when available.
    target_site = RUNTIME_SITE_PACKAGES_DIR
    target_root = os.path.dirname(target_site)

    try:
        _append_tiler_log(
            "warning",
            "Runtime root mismatch detected; aligning module roots",
            virtughan=current.get("virtughan", ""),
            rasterio=current.get("rasterio", ""),
            rio_tiler=current.get("rio_tiler", ""),
            target_root=target_root,
        )

        # Prioritize target runtime root in import order.
        for p in (target_site, target_root):
            try:
                while p in sys.path:
                    sys.path.remove(p)
            except Exception:
                pass
        sys.path.insert(0, target_root)
        sys.path.insert(0, target_site)

        # Clear only modules that participate in the mismatch.
        for mod_name in list(sys.modules.keys()):
            if mod_name == "virtughan" or mod_name.startswith("virtughan."):
                del sys.modules[mod_name]
            elif mod_name == "rasterio" or mod_name.startswith("rasterio."):
                del sys.modules[mod_name]
            elif mod_name == "rio_tiler" or mod_name.startswith("rio_tiler."):
                del sys.modules[mod_name]

        importlib.invalidate_caches()
        _patch_rasterio_parsed_path_compat()

        # Rebind processor classes from aligned imports.
        TileProcessor, TP_path = _find_tileprocessor()
        TileProcessor.apply_colormap = staticmethod(_safe_apply_colormap)
        _patch_tileprocessor_fetch_tile_compat()
        processor = TileProcessor(cache_time=60)

        after = {
            "virtughan": _module_file("virtughan"),
            "rasterio": _module_file("rasterio"),
            "rio_tiler": _module_file("rio_tiler"),
        }
        _append_tiler_log(
            "info",
            "Runtime root alignment result",
            before=json.dumps(current),
            after=json.dumps(after),
        )
    except Exception as exc:
        _append_tiler_log("warning", "Runtime root alignment failed", detail=str(exc))


def _stabilize_startup_path_bindings() -> None:
    """Run a narrow one-time startup stabilization for rasterio parse-path bindings."""
    global processor, TileProcessor, TP_path

    try:
        before = _runtime_state_snapshot()

        rp = importlib.import_module("rasterio._path")
        rpath = importlib.import_module("rasterio.path")
        try:
            rp = importlib.reload(rp)
        except Exception:
            # If reload is unavailable or fails, continue with current module object.
            pass
        try:
            rpath = importlib.reload(rpath)
        except Exception:
            pass

        # Force both rasterio path entry points to share the same patched callables.
        parse_path = getattr(rp, "parse_path", None)
        parse_path_private = getattr(rp, "_parse_path", None)
        if parse_path is not None:
            try:
                setattr(rpath, "parse_path", parse_path)
            except Exception:
                pass
        if parse_path_private is not None:
            try:
                setattr(rpath, "_parse_path", parse_path_private)
            except Exception:
                pass

        # Rebind already-loaded aliases to the active rasterio._path callables.
        for mod_name, mod in list(sys.modules.items()):
            if not (mod_name.startswith("rio_tiler") or mod_name.startswith("virtughan")):
                continue
            try:
                if parse_path is not None and hasattr(mod, "parse_path"):
                    setattr(mod, "parse_path", parse_path)
                if parse_path_private is not None and hasattr(mod, "_parse_path"):
                    setattr(mod, "_parse_path", parse_path_private)
            except Exception:
                continue

        # Force rio_tiler + all virtughan modules to re-import from patched rasterio.path.
        for mod_name in list(sys.modules.keys()):
            if mod_name == "rio_tiler" or mod_name.startswith("rio_tiler."):
                del sys.modules[mod_name]
                continue
            if mod_name == "virtughan" or mod_name.startswith("virtughan."):
                del sys.modules[mod_name]
                continue

        importlib.invalidate_caches()
        _patch_rasterio_parsed_path_compat()
        TileProcessor, TP_path = _find_tileprocessor()
        TileProcessor.apply_colormap = staticmethod(_safe_apply_colormap)
        _patch_tileprocessor_fetch_tile_compat()
        processor = TileProcessor(cache_time=60)

        after = _runtime_state_snapshot()
        changed = []
        for key in sorted(set(before.keys()) | set(after.keys())):
            if str(before.get(key)) != str(after.get(key)):
                changed.append(key)

        _append_tiler_log(
            "info",
            "Startup ParsedPath stabilization",
            fields=",".join(changed) if changed else "none",
            detail=json.dumps({"before": before, "after": after}, default=str),
        )
    except Exception as exc:
        _append_tiler_log("warning", "Startup ParsedPath stabilization failed", detail=str(exc))


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
    """Return a semaphore bound to current event loop and generation epoch."""
    loop = asyncio.get_running_loop()
    key = (id(loop), int(_SEMAPHORE_EPOCH))
    sem = _TILE_REQUEST_SEMAPHORES.get(key)
    if sem is None:
        sem = asyncio.Semaphore(max(_TILER_CONCURRENCY, _MAX_DYNAMIC_CONCURRENCY))
        _TILE_REQUEST_SEMAPHORES[key] = sem
    return sem


def _register_request_task(generation: int) -> Optional[asyncio.Task]:
    try:
        task = asyncio.current_task()
    except Exception:
        return None
    if task is None:
        return None
    bucket = _GENERATION_REQUEST_TASKS.setdefault(int(generation), set())
    bucket.add(task)
    return task


def _unregister_request_task(generation: int, task: Optional[asyncio.Task]) -> None:
    if task is None:
        return
    bucket = _GENERATION_REQUEST_TASKS.get(int(generation))
    if not bucket:
        return
    try:
        bucket.discard(task)
    except Exception:
        pass
    if not bucket:
        _GENERATION_REQUEST_TASKS.pop(int(generation), None)


def _cancel_stale_request_tasks(current_generation: int) -> int:
    cancelled = 0
    for gen, tasks in list(_GENERATION_REQUEST_TASKS.items()):
        if int(gen) >= int(current_generation):
            continue
        for task in list(tasks):
            try:
                if not task.done():
                    task.cancel()
                    cancelled += 1
            except Exception:
                continue
    return cancelled


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


def _defer_during_settle_response(reason: str = "view_settling") -> Response:
    """Return a retryable response while view/viewport state is not ready yet."""
    return _retryable_tile_unavailable(reason, status_code=503)


def _tile_bounds_lonlat(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    n = 2 ** int(z)
    lon_left = (float(x) / n) * 360.0 - 180.0
    lon_right = ((float(x) + 1.0) / n) * 360.0 - 180.0
    lat_top = math.degrees(math.atan(math.sinh(math.pi * (1.0 - (2.0 * float(y) / n)))))
    lat_bottom = math.degrees(math.atan(math.sinh(math.pi * (1.0 - (2.0 * (float(y) + 1.0) / n)))))
    min_lon = min(lon_left, lon_right)
    max_lon = max(lon_left, lon_right)
    min_lat = min(lat_bottom, lat_top)
    max_lat = max(lat_bottom, lat_top)
    return (min_lon, min_lat, max_lon, max_lat)


def _lon_to_tile_x(lon: float, z: int) -> float:
    n = 2.0 ** int(z)
    return (float(lon) + 180.0) / 360.0 * n


def _lat_to_tile_y(lat: float, z: int) -> float:
    n = 2.0 ** int(z)
    lat_clamped = max(-85.05112878, min(85.05112878, float(lat)))
    lat_rad = math.radians(lat_clamped)
    return (1.0 - math.log(math.tan(lat_rad) + (1.0 / math.cos(lat_rad))) / math.pi) / 2.0 * n


def _tile_intersects_settled_viewport(z: int, x: int, y: int, generation: int) -> bool:
    vp = _SETTLED_VIEWPORT
    if not vp:
        return True
    try:
        vp_gen = int(vp.get("generation", -1))
    except Exception:
        vp_gen = -1
    if vp_gen != int(generation):
        return True

    try:
        min_lon = float(vp["min_lon"])
        min_lat = float(vp["min_lat"])
        max_lon = float(vp["max_lon"])
        max_lat = float(vp["max_lat"])
        pad_deg = max(0.0, float(vp.get("pad_deg", 0.0)))
        pad_tiles = max(0, int(vp.get("pad_tiles", 0)))
    except Exception:
        return True

    try:
        tile_x = int(x)
        tile_y = int(y)
        x0 = int(math.floor(_lon_to_tile_x(min_lon, z))) - pad_tiles
        x1 = int(math.floor(_lon_to_tile_x(max_lon, z))) + pad_tiles
        y0 = int(math.floor(_lat_to_tile_y(max_lat, z))) - pad_tiles
        y1 = int(math.floor(_lat_to_tile_y(min_lat, z))) + pad_tiles

        n = (2 ** int(z)) - 1
        x0 = max(0, min(n, x0))
        x1 = max(0, min(n, x1))
        y0 = max(0, min(n, y0))
        y1 = max(0, min(n, y1))
        if x0 <= x1 and y0 <= y1 and x0 <= tile_x <= x1 and y0 <= tile_y <= y1:
            return True

        # Fallback to geometric intersection in lon/lat to avoid false negatives
        # near tile edges after pan/zoom settle.
        t_min_lon, t_min_lat, t_max_lon, t_max_lat = _tile_bounds_lonlat(z=z, x=tile_x, y=tile_y)
        n_tiles = max(1, 2 ** int(z))
        lon_per_tile = 360.0 / float(n_tiles)
        lat_per_tile = max(0.0, t_max_lat - t_min_lat)
        vp_min_lon = min_lon - pad_deg - (pad_tiles * lon_per_tile)
        vp_max_lon = max_lon + pad_deg + (pad_tiles * lon_per_tile)
        vp_min_lat = min_lat - pad_deg - (pad_tiles * lat_per_tile)
        vp_max_lat = max_lat + pad_deg + (pad_tiles * lat_per_tile)

        no_overlap = (
            (t_max_lon < vp_min_lon)
            or (t_min_lon > vp_max_lon)
            or (t_max_lat < vp_min_lat)
            or (t_min_lat > vp_max_lat)
        )
        return not no_overlap
    except Exception:
        return True


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


def _looks_like_parsed_path_error(value) -> bool:
    text = str(value or "")
    lowered = text.lower()
    return ("parsedpath" in lowered) or ("invalid path" in lowered and "scheme=" in lowered and "path=" in lowered)


def _refresh_parsedpath_guards() -> None:
    global processor, _PARSEDPATH_RECOVERY_IN_PROGRESS, _LAST_PARSEDPATH_RECOVERY_MONOTONIC
    now = time.monotonic()
    if _PARSEDPATH_RECOVERY_IN_PROGRESS:
        _append_tiler_log("info", "Skipped ParsedPath refresh: recovery already in progress")
        return
    if (now - _LAST_PARSEDPATH_RECOVERY_MONOTONIC) < _PARSEDPATH_RECOVERY_COOLDOWN_SEC:
        _append_tiler_log("info", "Skipped ParsedPath refresh: cooldown active")
        return

    _PARSEDPATH_RECOVERY_IN_PROGRESS = True
    _LAST_PARSEDPATH_RECOVERY_MONOTONIC = now
    try:
        before = _runtime_state_snapshot()
        _append_tiler_log("warning", "Refreshing ParsedPath guards")
        _patch_rasterio_parsed_path_compat()
        processor = TileProcessor(cache_time=60)
        after = _runtime_state_snapshot()
        changed = []
        for key in sorted(set(before.keys()) | set(after.keys())):
            if str(before.get(key)) != str(after.get(key)):
                changed.append(key)
        _append_tiler_log(
            "info",
            "ParsedPath refresh state diff",
            fields=",".join(changed) if changed else "none",
            detail=json.dumps({"before": before, "after": after}, default=str),
        )
    finally:
        _PARSEDPATH_RECOVERY_IN_PROGRESS = False


async def _generate_tile_uncached(**kwargs):
    raw_func = getattr(processor.cached_generate_tile, "__wrapped__", None)
    if raw_func is not None:
        return await raw_func(processor, **kwargs)
    return await processor.cached_generate_tile(**kwargs)


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
async def bump_generation(
    reason: str = Query("view_changed"),
    settle: bool = Query(True),
    settle_delay_sec: Optional[float] = Query(None),
):
    global _VIEW_GENERATION, _SEMAPHORE_EPOCH, _LAST_VIEW_CHANGE_MONOTONIC, _VIEW_SETTLE_DELAY_SEC
    if str(reason) == "view_settled":
        _append_tiler_log(
            "info",
            "View generation settled",
            reason=reason,
            generation=_VIEW_GENERATION,
            settle=bool(settle),
            cancelled_tasks=0,
        )
        return {
            "generation": _VIEW_GENERATION,
            "reason": reason,
            "settle": bool(settle),
            "cancelled_tasks": 0,
        }

    _VIEW_GENERATION += 1
    _SEMAPHORE_EPOCH = int(_VIEW_GENERATION)
    # Do not force-cancel in-flight request tasks on every pan/zoom bump.
    # Aggressive cancellation causes visible tile holes when map motion is frequent.
    cancelled_tasks = 0
    # Rotate semaphores so new viewport requests do not wait behind stale generations.
    _TILE_REQUEST_SEMAPHORES.clear()
    if settle:
        if settle_delay_sec is not None:
            try:
                _VIEW_SETTLE_DELAY_SEC = max(0.0, float(settle_delay_sec))
            except Exception:
                _VIEW_SETTLE_DELAY_SEC = _DEFAULT_VIEW_SETTLE_DELAY_SEC
        _LAST_VIEW_CHANGE_MONOTONIC = time.monotonic()
    _append_tiler_log(
        "info",
        "View generation bumped",
        reason=reason,
        generation=_VIEW_GENERATION,
        settle=bool(settle),
        cancelled_stale_tasks=int(cancelled_tasks),
        settle_delay_sec=round(float(_VIEW_SETTLE_DELAY_SEC), 3),
    )
    return {
        "generation": _VIEW_GENERATION,
        "reason": reason,
        "settle": bool(settle),
        "cancelled_stale_tasks": int(cancelled_tasks),
        "settle_delay_sec": float(_VIEW_SETTLE_DELAY_SEC),
    }


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


@app.post("/diag/set-viewport")
@app.get("/diag/set-viewport")
async def set_viewport(
    generation: int = Query(...),
    min_lon: float = Query(...),
    min_lat: float = Query(...),
    max_lon: float = Query(...),
    max_lat: float = Query(...),
    pad_deg: float = Query(0.02),
    pad_tiles: int = Query(0),
):
    global _SETTLED_VIEWPORT
    lo_lon = min(float(min_lon), float(max_lon))
    hi_lon = max(float(min_lon), float(max_lon))
    lo_lat = min(float(min_lat), float(max_lat))
    hi_lat = max(float(min_lat), float(max_lat))
    _SETTLED_VIEWPORT = {
        "generation": int(generation),
        "min_lon": lo_lon,
        "min_lat": lo_lat,
        "max_lon": hi_lon,
        "max_lat": hi_lat,
        "pad_deg": max(0.0, float(pad_deg)),
        "pad_tiles": max(0, int(pad_tiles)),
        "updated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    _append_tiler_log(
        "info",
        (
            "Settled viewport updated "
            f"gen={int(generation)} "
            f"bbox=({lo_lon:.6f},{lo_lat:.6f},{hi_lon:.6f},{hi_lat:.6f}) "
            f"pad_tiles={max(0, int(pad_tiles))}"
        ),
        **_SETTLED_VIEWPORT,
    )
    return _SETTLED_VIEWPORT


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
    global _ACTIVE_TILE_REQUESTS
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
    request_task = _register_request_task(request_generation)
    req_url = str(request.url)
    _append_tile_url_sample(tile=req_tag, full_url=req_url)
    sem = _get_request_semaphore()

    acquired = False
    counted_active = False
    compute_task: Optional[asyncio.Task] = None
    try:
        if _is_view_settling():
            wait_sec = _view_settle_remaining_sec()
            if wait_sec > 0.0:
                _append_tile_event_log("info", "Deferred tile while view is settling", tile=req_tag, wait_sec=round(wait_sec, 3))
                return _defer_during_settle_response("view_settling")

        # Fast reject before queueing when tile is outside the settled viewport.
        # This keeps worker slots available for likely-visible tiles.
        if not _tile_intersects_settled_viewport(z=z, x=x, y=y, generation=request_generation):
            vp = _SETTLED_VIEWPORT or {}
            _append_tile_event_log(
                "info",
                "Deferred tile outside settled viewport",
                tile=req_tag,
                request_generation=int(request_generation),
                settled_generation=vp.get("generation"),
                min_lon=vp.get("min_lon"),
                min_lat=vp.get("min_lat"),
                max_lon=vp.get("max_lon"),
                max_lat=vp.get("max_lat"),
                pad_deg=vp.get("pad_deg"),
                pad_tiles=vp.get("pad_tiles"),
            )
            return _defer_during_settle_response("outside_settled_viewport")

        current_limit = _dynamic_concurrency_limit()
        _append_tile_event_log(
            "info",
            "Tile queued; waiting for worker",
            tile=req_tag,
            limit=current_limit,
        )
        waited = 0.0
        while waited < _QUEUE_WAIT_TIMEOUT_SEC and not acquired:
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

        compute_task = asyncio.create_task(
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
            )
        )
        elapsed = 0.0
        image_bytes = None
        feature = None
        while True:
            remaining = _TILE_COMPUTE_TIMEOUT_SEC - elapsed
            if remaining <= 0.0:
                compute_task.cancel()
                raise asyncio.TimeoutError()

            slice_sec = min(_STALE_CHECK_SLICE_SEC, remaining)
            try:
                image_bytes, feature = await asyncio.wait_for(asyncio.shield(compute_task), timeout=slice_sec)
                break
            except asyncio.TimeoutError:
                elapsed += slice_sec
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
        if _looks_like_parsed_path_error(getattr(he, "detail", "")):
            _append_tile_event_log(
                "warning",
                "ParsedPath HTTPException detected; attempting in-request recovery",
                tile=req_tag,
                detail=str(getattr(he, "detail", "")),
            )
            try:
                _refresh_parsedpath_guards()
                image_bytes, feature = await asyncio.wait_for(
                    _generate_tile_uncached(
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
                headers = {"Cache-Control": "public, max-age=300"}
                try:
                    props = feature.get("properties", {})
                    if "datetime" in props:
                        headers["X-Image-Date"] = props["datetime"]
                    if "eo:cloud_cover" in props:
                        headers["X-Cloud-Cover"] = str(props["eo:cloud_cover"])
                except Exception:
                    pass
                _LAST_TILE_ERROR.clear()
                _append_tile_event_log("info", "Served tile after parsed-path HTTPException recovery", tile=req_tag)
                return Response(content=image_bytes, media_type="image/png", headers=headers)
            except Exception as retry_ex:
                return JSONResponse(status_code=500, content={"detail": str(retry_ex)})
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
        try:
            if compute_task is not None and not compute_task.done():
                compute_task.cancel()
        except Exception:
            pass
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
        if _looks_like_parsed_path_error(ex):
            _append_tile_event_log(
                "warning",
                "ParsedPath error detected; attempting in-request recovery",
                tile=req_tag,
                detail=str(ex),
            )
            try:
                _refresh_parsedpath_guards()
                image_bytes, feature = await asyncio.wait_for(
                    _generate_tile_uncached(
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
                headers = {"Cache-Control": "public, max-age=300"}
                try:
                    props = feature.get("properties", {})
                    if "datetime" in props:
                        headers["X-Image-Date"] = props["datetime"]
                    if "eo:cloud_cover" in props:
                        headers["X-Cloud-Cover"] = str(props["eo:cloud_cover"])
                except Exception:
                    pass
                _LAST_TILE_ERROR.clear()
                _append_tile_event_log("info", "Served tile after parsed-path recovery", tile=req_tag)
                return Response(content=image_bytes, media_type="image/png", headers=headers)
            except Exception:
                pass

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
        _unregister_request_task(request_generation, request_task)
        if acquired:
            try:
                sem.release()
            except Exception:
                pass
        if counted_active and _ACTIVE_TILE_REQUESTS > 0:
            _ACTIVE_TILE_REQUESTS -= 1
