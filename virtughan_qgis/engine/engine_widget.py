# virtughan_qgis/engine/engine_widget.py
import os
import sys
import time
import uuid
import traceback
import json
import shutil
import tempfile
import subprocess
import threading
import multiprocessing
import importlib
from datetime import datetime
from pathlib import Path
from contextlib import contextmanager

from qgis.PyQt import uic
from qgis.PyQt.QtCore import Qt, QDate, QTimer, QVariant
from qgis.PyQt.QtGui import QColor, QCursor, QPixmap
from qgis.PyQt.QtWidgets import (
    QWidget, QDockWidget, QFileDialog, QMessageBox,
    QProgressBar, QPlainTextEdit, QComboBox, QCheckBox, QLabel,
    QPushButton, QSpinBox, QLineEdit, QDateEdit, QFormLayout, QVBoxLayout, QHBoxLayout, QRadioButton,
    QScrollArea
)

from qgis.core import (
    Qgis,
    QgsMessageLog,
    QgsProcessingUtils,
    QgsGeometry,
    QgsProject,
    QgsRectangle,
    QgsRasterLayer,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsField,
    QgsPointXY,
    QgsVectorLayer,
    QgsApplication,
    QgsTask,
)

from ..common.aoi import (
    AoiManager,
    AoiPolygonTool,
    AoiRectTool,
    rect_to_wgs84_bbox,
    geom_to_wgs84_bbox,
)
from ..common.common_logic import (
    index_presets_two_band,
    get_index_preset,
    match_index_preset,
    load_bands_meta,
    populate_band_combos,
)

from ..common.map_setup import setup_default_map
from ..common.scene_preview_dialog import ScenePreviewDialog
from ..bootstrap import (
    RUNTIME_ROOT,
    RUNTIME_SITE_PACKAGES_DIR,
    RUNTIME_FALLBACK_ROOT,
    RUNTIME_FALLBACK_SITE_PACKAGES_DIR,
    ensure_runtime_network_ready,
)

COMMON_IMPORT_ERROR = None
CommonParamsWidget = None
try:
    from ..common.common_widget import (
        CommonParamsWidget,
        extract_zipfiles
    )
    
except Exception as _e:
    COMMON_IMPORT_ERROR = _e
    CommonParamsWidget = None

engine_search_stac_api = None
_COMPUTE_SUBPROCESS_RUN_COUNT = 0


def _resolve_engine_search_stac_api():
    global engine_search_stac_api
    if callable(engine_search_stac_api):
        return engine_search_stac_api
    for module_name in ("virtughan.extract", "virtughan.engine"):
        backend = sys.modules.get(module_name)
        if backend is None:
            continue
        for symbol_name in ("search_stac_api", "search_stac"):
            candidate = getattr(backend, symbol_name, None)
            if callable(candidate):
                engine_search_stac_api = candidate
                return candidate
    return None

UI_PATH = os.path.join(os.path.dirname(__file__), "engine_form.ui")
FORM_CLASS, _ = uic.loadUiType(UI_PATH)


class _TaskCancelledError(RuntimeError):
    pass


def _log(widget, msg, level=Qgis.Info):
    QgsMessageLog.logMessage(str(msg), "VirtuGhan", level)
    try:
        widget.logText.appendPlainText(str(msg))
    except Exception:
        pass


def _is_transient_raster_read_failure(exc, log_path: str | None = None) -> bool:
    signatures = (
        "Read failed",
        "RasterioIOError",
        "IReadBlock failed",
        "TIFFReadEncodedTile",
        "TIFFFillTile",
        "CPLE_AppDefinedError",
    )

    text = str(exc or "")
    if any(sig in text for sig in signatures):
        return True

    if not log_path or not os.path.exists(log_path):
        return False

    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            tail = f.read()[-20000:]
        return any(sig in tail for sig in signatures)
    except Exception:
        return False


def _build_engine_failure_message(exc, log_path: str | None = None) -> str:
    if _is_transient_raster_read_failure(exc, log_path=log_path):
        return (
            "Compute failed after multiple retries while reading remote raster tiles.\n\n"
            "This is usually a temporary network/data-access issue (internet instability, VPN/proxy/firewall interruption, or remote server throttling).\n\n"
            "Please check your internet connection and try again.\n"
            "If the issue persists, try a smaller AOI or shorter date range and review runtime.log."
        )

    text = str(exc or "")
    if "SVD did not converge in Linear Least Squares" in text:
        return (
            "Compute failed while fitting trend statistics on the selected scenes.\n\n"
            "This usually happens when there are too few valid observations or unstable/invalid values for the selected AOI/date/formula.\n\n"
            "Try one or more of the following:\n"
            "- Expand the date range (to include more scenes)\n"
            "- Reduce cloud threshold and/or enable Smart Filter\n"
            "- Use a different formula or simpler AOI\n"
            "- Retry with a nearby date window\n\n"
            "See runtime.log for details."
        )

    return f"Compute failed:\n{exc}\n\nSee runtime.log for details."


def _resolve_embedded_python_executable() -> str:
    candidates: list[Path] = []
    install_roots: list[Path] = []

    def _add_root(path_value):
        try:
            p = Path(path_value).resolve()
        except Exception:
            return
        if p not in install_roots:
            install_roots.append(p)

    def _add_candidate(path_value):
        try:
            p = Path(path_value)
        except Exception:
            return
        candidates.append(p)

    def _is_within_install_roots(candidate_path: Path) -> bool:
        try:
            resolved = candidate_path.resolve()
        except Exception:
            return False
        for root in install_roots:
            try:
                resolved.relative_to(root)
                return True
            except Exception:
                continue
        return False

    prefix = QgsApplication.prefixPath() or ""
    if prefix:
        prefix_path = Path(prefix).resolve()
        _add_root(prefix_path)
        for i, parent in enumerate(prefix_path.parents):
            if i >= 4:
                break
            _add_root(parent)

    if sys.executable:
        try:
            exe_path = Path(sys.executable).resolve()
            _add_root(exe_path.parent)
            for i, parent in enumerate(exe_path.parents):
                if i >= 5:
                    break
                _add_root(parent)

            if sys.platform == "darwin":
                for parent in exe_path.parents:
                    if parent.suffix.lower() == ".app":
                        _add_root(parent)
                        _add_root(parent / "Contents")
                        _add_root(parent / "Contents" / "MacOS")
                        break
        except Exception:
            pass

    base_executable = getattr(sys, "_base_executable", "")
    if base_executable:
        _add_candidate(base_executable)
    env_python = os.environ.get("PYTHONEXECUTABLE")
    if env_python:
        _add_candidate(env_python)

    if prefix:
        prefix_path = Path(prefix)
        candidates.extend(
            [
                prefix_path / "python.exe",
                prefix_path / "python3",
                prefix_path / "python",
                prefix_path / "bin" / "python.exe",
                prefix_path / "bin" / "python3",
                prefix_path / "bin" / "python",
            ]
        )
        try:
            bin_dir = prefix_path / "bin"
            if bin_dir.is_dir():
                for entry in sorted(bin_dir.iterdir()):
                    if entry.is_file() and entry.name.lower().startswith("python"):
                        candidates.append(entry)
        except Exception:
            pass

    if sys.platform == "darwin" and sys.executable:
        try:
            exe_path = Path(sys.executable).resolve()
            for parent in exe_path.parents:
                if parent.name == "MacOS":
                    app_contents = parent.parent
                    candidates.extend(
                        [
                            parent / "python",
                            parent / "python3",
                            parent / "python3.12",
                            parent / "bin" / "python3",
                            parent / "bin" / "python",
                            app_contents / "Frameworks" / "Python.framework" / "Versions" / "Current" / "bin" / "python3",
                            app_contents / "Frameworks" / "Python.framework" / "Versions" / "Current" / "bin" / "python",
                            app_contents / "Resources" / "python" / "bin" / "python3",
                            app_contents / "Resources" / "python" / "bin" / "python",
                        ]
                    )
                    try:
                        for entry in sorted(parent.iterdir()):
                            if entry.is_file() and entry.name.lower().startswith("python"):
                                candidates.append(entry)
                    except Exception:
                        pass
                    break
        except Exception:
            pass

    if os.name == "nt":
        for root in list(install_roots):
            apps_dir = root / "apps"
            if not apps_dir.is_dir():
                continue
            try:
                for app in sorted(apps_dir.iterdir()):
                    if app.is_dir() and app.name.lower().startswith("python"):
                        candidates.append(app / "python.exe")
                        candidates.append(app / "Scripts" / "python.exe")
            except Exception:
                pass

    if sys.executable:
        _add_candidate(sys.executable)

    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception:
            continue
        normalized = str(resolved)
        if normalized in seen:
            continue
        seen.add(normalized)

        name = resolved.name.lower()
        if name in {"qgis", "qgis-bin.exe", "qgis-ltr-bin.exe"}:
            continue
        if sys.platform == "darwin":
            posix_path = resolved.as_posix().lower()
            if "/contents/resources/scripts/python" in posix_path:
                # This wrapper can point to a missing versioned python executable.
                continue
        if "python" not in name:
            continue
        if not resolved.is_file():
            continue
        if not _is_within_install_roots(resolved):
            continue
        if os.name != "nt" and not os.access(str(resolved), os.X_OK):
            continue
        return normalized

    return ""


def _configure_geospatial_runtime(logf=None):
    os.environ.setdefault("GDAL_HTTP_TIMEOUT", "30")
    os.environ.setdefault("GDAL_HTTP_MAX_RETRY", "8")
    os.environ.setdefault("GDAL_HTTP_RETRY_DELAY", "2")
    os.environ.setdefault("GDAL_HTTP_MULTIRANGE", "NO")
    os.environ.setdefault("CPL_VSIL_CURL_USE_HEAD", "NO")
    os.environ.setdefault("VSI_CACHE", "TRUE")
    os.environ.setdefault("VSI_CACHE_SIZE", "50000000")
    os.environ.setdefault("CPL_DEBUG", "ON")

    prefix = QgsApplication.prefixPath() or ""
    proj_candidates = [
        os.path.join(prefix, "share", "proj"),
        os.path.normpath(os.path.join(prefix, "..", "share", "proj")),
        os.path.normpath(os.path.join(prefix, "..", "..", "share", "proj")),
        "/Applications/QGIS.app/Contents/Resources/proj",
    ]

    for candidate in proj_candidates:
        proj_db = os.path.join(candidate, "proj.db")
        if os.path.isfile(proj_db):
            os.environ["PROJ_LIB"] = candidate
            os.environ["PROJ_DATA"] = candidate
            if logf:
                logf.write(f"Using PROJ data path: {candidate}\n")
            break


def _apply_transient_read_fallback(logf=None):
    fallback_values = {
        "VSI_CACHE": "FALSE",
        "CPL_VSIL_CURL_CACHE_SIZE": "0",
        "GDAL_HTTP_MULTIRANGE": "SINGLE_GET",
        "GDAL_HTTP_MAX_RETRY": "12",
        "GDAL_HTTP_RETRY_DELAY": "3",
    }

    for key, value in fallback_values.items():
        os.environ[key] = value

    if logf:
        logf.write(
            "Applied transient read fallback settings: "
            + ", ".join(f"{k}={v}" for k, v in fallback_values.items())
            + "\n"
        )


def _engine_compute_fallback_worker(params: dict, log_path: str, result_queue):
    try:
        runtime_pairs = [
            (RUNTIME_SITE_PACKAGES_DIR, RUNTIME_ROOT),
            (RUNTIME_FALLBACK_SITE_PACKAGES_DIR, RUNTIME_FALLBACK_ROOT),
        ]
        chosen_paths = []
        for site_pkgs, root in runtime_pairs:
            if not site_pkgs or not os.path.isdir(site_pkgs):
                continue
            if os.path.isdir(os.path.join(site_pkgs, "virtughan")):
                chosen_paths = [site_pkgs]
                if root and os.path.isdir(root):
                    chosen_paths.append(root)
                break

        if not chosen_paths:
            for site_pkgs, root in runtime_pairs:
                if site_pkgs and os.path.isdir(site_pkgs):
                    chosen_paths = [site_pkgs]
                    if root and os.path.isdir(root):
                        chosen_paths.append(root)
                    break

        for dep_path in chosen_paths:
            while dep_path in sys.path:
                try:
                    sys.path.remove(dep_path)
                except Exception:
                    break
            sys.path.insert(0, dep_path)

        for mod_name in list(sys.modules.keys()):
            if mod_name == "virtughan.engine" or mod_name.startswith("virtughan.engine."):
                try:
                    del sys.modules[mod_name]
                except Exception:
                    pass

        importlib.invalidate_caches()
        _backend_mod = importlib.import_module("virtughan.engine")
        backend_cls = getattr(_backend_mod, "VirtughanProcessor")

        _configure_geospatial_runtime(logf=None)
        cpl_log_path = os.path.join(params["output_dir"], "gdal.log")
        os.environ["CPL_LOG"] = cpl_log_path

        with open(log_path, "a", encoding="utf-8", buffering=1) as worker_log:
            worker_log.write("[WARNING] macOS fallback: running compute in forked process because external Python preflight failed.\n")
            proc = backend_cls(
                bbox=params["bbox"],
                start_date=params["start_date"],
                end_date=params["end_date"],
                cloud_cover=params["cloud_cover"],
                formula=params["formula"],
                band1=params["band1"],
                band2=params["band2"],
                operation=params["operation"],
                timeseries=params["timeseries"],
                output_dir=params["output_dir"],
                log_file=worker_log,
                cmap="RdYlGn",
                workers=params["workers"],
                smart_filter=params["smart_filter"],
            )
            proc.compute()
        result_queue.put({"ok": True})
    except Exception:
        details = traceback.format_exc()
        try:
            with open(log_path, "a", encoding="utf-8", buffering=1) as worker_log:
                worker_log.write("[fallback_worker_exception]\n")
                worker_log.write(details)
        except Exception:
            pass
        result_queue.put({"ok": False, "error": details})


def _run_engine_inprocess_fallback(params: dict, log_path: str, logf=None, should_cancel=None):
    if logf:
        logf.write("[WARNING] macOS fallback: switching from external subprocess to spawned process.\n")

    fallback_exe = _resolve_embedded_python_executable()
    if fallback_exe:
        try:
            multiprocessing.set_executable(fallback_exe)
            if logf:
                logf.write(f"[INFO] macOS fallback multiprocessing executable: {fallback_exe}\n")
        except Exception as exc:
            if logf:
                logf.write(f"[WARNING] could not set fallback multiprocessing executable: {exc}\n")

    ctx = multiprocessing.get_context("spawn")
    result_queue = ctx.Queue()
    proc = ctx.Process(
        target=_engine_compute_fallback_worker,
        args=(params, log_path, result_queue),
        daemon=False,
    )
    proc.start()

    deadline = time.monotonic() + 900.0
    while proc.is_alive():
        if callable(should_cancel) and should_cancel():
            try:
                proc.terminate()
            except Exception:
                pass
            proc.join(timeout=2.0)
            raise _TaskCancelledError("Compute cancelled by user.")
        if time.monotonic() > deadline:
            try:
                proc.terminate()
            except Exception:
                pass
            proc.join(timeout=2.0)
            raise RuntimeError("Compute fallback process timed out after 15 minutes.")
        time.sleep(0.2)

    proc.join(timeout=1.0)
    result = None
    try:
        result = result_queue.get_nowait()
    except Exception:
        result = None

    if isinstance(result, dict) and result.get("ok"):
        return

    if isinstance(result, dict) and result.get("error"):
        raise RuntimeError(result.get("error"))

    # On macOS fork fallback, child can occasionally terminate with a late native-code
    # signal after outputs are already written. Treat this as success if outputs exist.
    if proc.exitcode in (-5, -6, -11):
        try:
            out_dir = params.get("output_dir", "")
            has_outputs = False
            if out_dir and os.path.isdir(out_dir):
                for _root, _dirs, files in os.walk(out_dir):
                    if any(fn.lower().endswith((".tif", ".tiff", ".vrt", ".png")) for fn in files):
                        has_outputs = True
                        break
            if has_outputs:
                if logf:
                    logf.write(
                        f"[WARNING] fallback child exited with code {proc.exitcode} after writing outputs; treating run as successful.\n"
                    )
                return
        except Exception:
            pass

    raise RuntimeError(f"Compute fallback process failed with exit code {proc.exitcode}.")


def _run_engine_inprocess_mac(params: dict, logf=None, should_cancel=None):
    with _with_embedded_python_executable(logf=logf):
        runtime_pairs = [
            (RUNTIME_SITE_PACKAGES_DIR, RUNTIME_ROOT),
            (RUNTIME_FALLBACK_SITE_PACKAGES_DIR, RUNTIME_FALLBACK_ROOT),
        ]

        chosen_paths = []
        for site_pkgs, root in runtime_pairs:
            if not site_pkgs or not os.path.isdir(site_pkgs):
                continue
            if os.path.isdir(os.path.join(site_pkgs, "virtughan")):
                chosen_paths = [site_pkgs]
                if root and os.path.isdir(root):
                    chosen_paths.append(root)
                break

        if not chosen_paths:
            for site_pkgs, root in runtime_pairs:
                if site_pkgs and os.path.isdir(site_pkgs):
                    chosen_paths = [site_pkgs]
                    if root and os.path.isdir(root):
                        chosen_paths.append(root)
                    break

        for dep_path in chosen_paths:
            while dep_path in sys.path:
                try:
                    sys.path.remove(dep_path)
                except Exception:
                    break
            sys.path.insert(0, dep_path)

        if callable(should_cancel) and should_cancel():
            raise _TaskCancelledError("Compute cancelled by user.")

        backend_mod = sys.modules.get("virtughan.engine")
        if backend_mod is None:
            backend_mod = importlib.import_module("virtughan.engine")
        else:
            backend_mod = importlib.reload(backend_mod)
        backend_cls = getattr(backend_mod, "VirtughanProcessor")

        workers = int(params.get("workers", 1) or 1)
        if workers > 1:
            if logf:
                logf.write(
                    "[INFO] macOS in-process compute: forcing workers=1 to avoid spawning extra QGIS windows.\n"
                )
            workers = 1

        proc = backend_cls(
            bbox=params["bbox"],
            start_date=params["start_date"],
            end_date=params["end_date"],
            cloud_cover=params["cloud_cover"],
            formula=params["formula"],
            band1=params["band1"],
            band2=params["band2"],
            operation=params["operation"],
            timeseries=params["timeseries"],
            output_dir=params["output_dir"],
            log_file=logf,
            cmap="RdYlGn",
            workers=workers,
            smart_filter=params["smart_filter"],
        )
        proc.compute()


@contextmanager
def _with_embedded_python_executable(logf=None):
    original_executable = sys.executable
    mp_set_executable = None
    original_python_executable_env = os.environ.get("PYTHONEXECUTABLE")
    embedded_python = _resolve_embedded_python_executable()
    try:
        if embedded_python and embedded_python != original_executable:
            sys.executable = embedded_python
            if logf:
                logf.write(f"Using embedded Python executable: {embedded_python}\n")
        elif logf:
            logf.write("Embedded Python executable not resolved; keeping current sys.executable\n")
        if embedded_python:
            os.environ["PYTHONEXECUTABLE"] = embedded_python
        try:
            import multiprocessing as _mp
            try:
                import multiprocessing.spawn as _mp_spawn
            except Exception:
                _mp_spawn = None

            if sys.platform == "darwin":
                try:
                    current_method = _mp.get_start_method(allow_none=True)
                except Exception:
                    current_method = None
                if current_method != "fork":
                    try:
                        _mp.set_start_method("fork", force=True)
                        if logf:
                            logf.write("Using multiprocessing start method: fork\n")
                    except Exception as start_method_exc:
                        if logf:
                            logf.write(f"Could not set multiprocessing start method to fork: {start_method_exc}\n")

            mp_set_executable = getattr(_mp, "set_executable", None)
            if not callable(mp_set_executable) and _mp_spawn is not None:
                mp_set_executable = getattr(_mp_spawn, "set_executable", None)

            if callable(mp_set_executable) and embedded_python:
                mp_set_executable(embedded_python)
                if logf:
                    logf.write(f"Using multiprocessing executable: {embedded_python}\n")
            elif embedded_python and _mp_spawn is not None:
                try:
                    _mp_spawn._python_exe = os.fsencode(embedded_python)
                    if logf:
                        logf.write(f"Using multiprocessing _python_exe fallback: {embedded_python}\n")
                except Exception:
                    pass
        except Exception as mp_exc:
            if logf:
                logf.write(f"Could not configure multiprocessing executable: {mp_exc}\n")
        yield
    finally:
        if original_python_executable_env is None:
            os.environ.pop("PYTHONEXECUTABLE", None)
        else:
            os.environ["PYTHONEXECUTABLE"] = original_python_executable_env
        sys.executable = original_executable


def _run_engine_in_subprocess(params: dict, log_path: str, logf=None, should_cancel=None):
    if sys.platform == "darwin":
        if logf:
            logf.write("[INFO] macOS guard: bypassing external subprocess and using in-process compute path.\n")
        _run_engine_inprocess_mac(params, logf=logf, should_cancel=should_cancel)
        return

    global _COMPUTE_SUBPROCESS_RUN_COUNT
    python_exe = _resolve_embedded_python_executable()
    if not python_exe:
        raise RuntimeError(
            "Could not locate an embedded Python executable for subprocess execution. "
            f"Current sys.executable={sys.executable}"
        )
    if sys.platform == "darwin" and Path(python_exe).name.lower() == "qgis":
        raise RuntimeError(
            "Refusing to launch subprocess with QGIS GUI executable on macOS. "
            f"Resolved path={python_exe}"
        )
    work_dir = tempfile.mkdtemp(prefix="virtughan-engine-")
    payload_path = os.path.join(work_dir, "payload.json")
    runner_path = os.path.join(work_dir, "runner.py")

    prefix = QgsApplication.prefixPath() or ""
    payload = {
        "params": params,
        "log_path": log_path,
        "proj_candidates": [
            os.path.join(prefix, "share", "proj"),
            os.path.normpath(os.path.join(prefix, "..", "share", "proj")),
            os.path.normpath(os.path.join(prefix, "..", "..", "share", "proj")),
            "/Applications/QGIS.app/Contents/Resources/proj",
        ],
    }

    runner_code = '''import json
import os
import sys
import time
import traceback


def _configure_runtime(payload, logf):
    os.environ.setdefault("GDAL_HTTP_TIMEOUT", "30")
    os.environ.setdefault("GDAL_HTTP_MAX_RETRY", "8")
    os.environ.setdefault("GDAL_HTTP_RETRY_DELAY", "2")
    os.environ.setdefault("GDAL_HTTP_MULTIRANGE", "NO")
    os.environ.setdefault("CPL_VSIL_CURL_USE_HEAD", "NO")
    os.environ.setdefault("VSI_CACHE", "TRUE")
    os.environ.setdefault("VSI_CACHE_SIZE", "50000000")
    os.environ.setdefault("CPL_DEBUG", "ON")

    for candidate in payload.get("proj_candidates", []):
        proj_db = os.path.join(candidate, "proj.db")
        if os.path.isfile(proj_db):
            os.environ["PROJ_LIB"] = candidate
            os.environ["PROJ_DATA"] = candidate
            logf.write(f"Using PROJ data path: {candidate}\\n")
            break


def _apply_transient_read_fallback(logf):
    fallback_values = {
        "VSI_CACHE": "FALSE",
        "CPL_VSIL_CURL_CACHE_SIZE": "0",
        "GDAL_HTTP_MULTIRANGE": "SINGLE_GET",
        "GDAL_HTTP_MAX_RETRY": "12",
        "GDAL_HTTP_RETRY_DELAY": "3",
    }
    for key, value in fallback_values.items():
        os.environ[key] = value
    logf.write(
        "Applied transient read fallback settings: "
        + ", ".join(f"{k}={v}" for k, v in fallback_values.items())
        + "\\n"
    )


def _is_transient_read_error(message, details):
    needles = (
        "TIFFReadEncodedTile",
        "IReadBlock failed",
        "RasterioIOError",
        "Read failed",
        "TIFFFillTile",
        "CPLE_AppDefinedError",
    )
    return any(n in message for n in needles) or any(n in details for n in needles)


def main():
    if len(sys.argv) < 2:
        return 2

    payload_path = sys.argv[1]
    with open(payload_path, "r", encoding="utf-8") as pf:
        payload = json.load(pf)

    params = payload["params"]
    log_path = payload["log_path"]
    cpl_log_path = os.path.join(params["output_dir"], "gdal.log")

    os.environ["CPL_LOG"] = cpl_log_path

    with open(log_path, "a", encoding="utf-8", buffering=1) as logf:
        _configure_runtime(payload, logf)
        logf.write(f"GDAL CPL_LOG: {cpl_log_path}\\n")
        logf.write(f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] Starting VirtughanProcessor\\n")
        logf.write(f"Params: {params}\\n")
        logf.write(f"PROJ_LIB: {os.environ.get('PROJ_LIB', '')}\\n")
        logf.write(f"PROJ_DATA: {os.environ.get('PROJ_DATA', '')}\\n")
        logf.write(f"sys.executable: {sys.executable}\\n")

        max_attempts = 4
        fallback_applied = False
        for attempt in range(1, max_attempts + 1):
            try:
                logf.write(f"Compute attempt {attempt}/{max_attempts}\\n")
                if attempt == 1:
                    logf.write("Please wait: preparing scenes and computing output (this may take few minutes depending on your area and time range).\\n")
                logf.write("Importing backend module virtughan.engine...\\n")
                from virtughan.engine import VirtughanProcessor
                logf.write("Imported VirtughanProcessor successfully.\\n")

                logf.write("Creating VirtughanProcessor instance...\\n")
                proc = VirtughanProcessor(
                    bbox=params["bbox"],
                    start_date=params["start_date"],
                    end_date=params["end_date"],
                    cloud_cover=params["cloud_cover"],
                    formula=params["formula"],
                    band1=params["band1"],
                    band2=params["band2"],
                    operation=params["operation"],
                    timeseries=params["timeseries"],
                    output_dir=params["output_dir"],
                    log_file=logf,
                    cmap="RdYlGn",
                    workers=params["workers"],
                    smart_filter=params["smart_filter"],
                )
                logf.write("Starting compute()...\\n")
                proc.compute()
                logf.write("compute() finished.\\n")
                return 0
            except Exception as exc:
                message = str(exc)
                details = traceback.format_exc()
                if attempt < max_attempts and _is_transient_read_error(message, details):
                    if not fallback_applied:
                        _apply_transient_read_fallback(logf)
                        fallback_applied = True
                    wait_s = 2 * attempt
                    logf.write(
                        f"Transient raster read error detected (attempt {attempt}/{max_attempts}); retrying in {wait_s}s...\\n"
                    )
                    time.sleep(wait_s)
                    continue

                logf.write("[subprocess_exception]\\n")
                logf.write(details)
                return 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
'''

    try:
        with open(payload_path, "w", encoding="utf-8") as pf:
            json.dump(payload, pf)
        with open(runner_path, "w", encoding="utf-8") as rf:
            rf.write(runner_code)

        if logf:
            logf.write(f"[INFO] launcher source file={__file__}\n")
            logf.write(f"[INFO] running engine in subprocess: {python_exe}\n")

        stdout_capture_path = os.path.join(work_dir, "subprocess_stdout.txt")
        stderr_capture_path = os.path.join(work_dir, "subprocess_stderr.txt")
        stdout_handle = open(stdout_capture_path, "w", encoding="utf-8", errors="replace")
        stderr_handle = open(stderr_capture_path, "w", encoding="utf-8", errors="replace")

        run_kwargs = {
            "args": [python_exe, runner_path, payload_path],
            "stdout": stdout_handle,
            "stderr": stderr_handle,
            "text": True,
        }

        env = os.environ.copy()
        
        runtime_pairs = [
            (RUNTIME_SITE_PACKAGES_DIR, RUNTIME_ROOT),
            (RUNTIME_FALLBACK_SITE_PACKAGES_DIR, RUNTIME_FALLBACK_ROOT),
        ]
        selected_runtime_paths = []
        for site_pkgs, root in runtime_pairs:
            if not site_pkgs or not os.path.isdir(site_pkgs):
                continue
            if os.path.isdir(os.path.join(site_pkgs, "virtughan")):
                selected_runtime_paths = [site_pkgs]
                break

        if not selected_runtime_paths:
            for site_pkgs, root in runtime_pairs:
                if site_pkgs and os.path.isdir(site_pkgs):
                    selected_runtime_paths = [site_pkgs]
                    break

        if selected_runtime_paths:
            env["PYTHONPATH"] = os.pathsep.join(selected_runtime_paths)
        else:
            env.pop("PYTHONPATH", None)

        if logf:
            logf.write(f"[INFO] subprocess PYTHONPATH={env.get('PYTHONPATH', '')}\n")

        env["PYTHONNOUSERSITE"] = "1"
        run_kwargs["env"] = env

        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0
            run_kwargs["startupinfo"] = startupinfo
            run_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        preflight_cmd = [python_exe, "-c", "import encodings,sys; print(sys.executable)"]
        preflight = None
        if sys.platform == "darwin":
            pyhome_candidates = [None]
            try:
                exe_path = Path(python_exe).resolve()
                app_contents = None
                for parent in exe_path.parents:
                    if parent.name == "Contents":
                        app_contents = parent
                        break
                if app_contents is not None:
                    for candidate in (
                        app_contents / "Resources" / "python",
                        app_contents / "Resources",
                        app_contents / "Frameworks" / "Python.framework" / "Versions" / "Current",
                    ):
                        if candidate.is_dir():
                            pyhome_candidates.append(str(candidate))
            except Exception:
                pass

            selected_env = None
            for pyhome in pyhome_candidates:
                candidate_env = env.copy()
                if pyhome:
                    candidate_env["PYTHONHOME"] = pyhome
                else:
                    candidate_env.pop("PYTHONHOME", None)

                preflight_run_kwargs = {
                    "stdout": subprocess.PIPE,
                    "stderr": subprocess.PIPE,
                    "text": True,
                    "env": candidate_env,
                    "timeout": 15,
                }
                if os.name == "nt":
                    preflight_startupinfo = subprocess.STARTUPINFO()
                    preflight_startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    preflight_startupinfo.wShowWindow = 0
                    preflight_run_kwargs["startupinfo"] = preflight_startupinfo
                    preflight_run_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

                preflight = subprocess.run(
                    preflight_cmd,
                    **preflight_run_kwargs,
                )
                if logf:
                    logf.write(
                        f"[INFO] mac preflight pyhome={pyhome or '<unset>'} returncode={preflight.returncode}\n"
                    )
                if preflight.returncode == 0:
                    selected_env = candidate_env
                    break

            if selected_env is None:
                stderr_text = (preflight.stderr or "").strip() if preflight else ""
                if "encodings" in stderr_text or "init_fs_encoding" in stderr_text:
                    _run_engine_inprocess_fallback(params, log_path, logf=logf, should_cancel=should_cancel)
                    return
                raise RuntimeError(
                    "Resolved Python executable failed startup preflight. "
                    f"path={python_exe}, returncode={preflight.returncode if preflight else 'n/a'}, "
                    f"stderr={stderr_text or 'n/a'}"
                )
            env = selected_env
            run_kwargs["env"] = env
        else:
            preflight_run_kwargs = {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
                "env": env,
                "timeout": 15,
            }
            if os.name == "nt":
                preflight_startupinfo = subprocess.STARTUPINFO()
                preflight_startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                preflight_startupinfo.wShowWindow = 0
                preflight_run_kwargs["startupinfo"] = preflight_startupinfo
                preflight_run_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

            preflight = subprocess.run(
                preflight_cmd,
                **preflight_run_kwargs,
            )
            if preflight.returncode != 0:
                raise RuntimeError(
                    "Resolved Python executable failed startup preflight. "
                    f"path={python_exe}, returncode={preflight.returncode}, stderr={preflight.stderr.strip()}"
                )

        if logf and preflight is not None:
            logf.write(f"[INFO] python preflight returncode={preflight.returncode}\n")
            if preflight.stdout:
                logf.write(f"[INFO] python preflight stdout={preflight.stdout.strip()}\n")
            if preflight.stderr:
                logf.write(f"[INFO] python preflight stderr={preflight.stderr.strip()}\n")

        proc = subprocess.Popen(**run_kwargs)
        stdout_data = ""
        stderr_data = ""
        try:
            deadline = time.monotonic() + 900.0
            is_first_subprocess_run = _COMPUTE_SUBPROCESS_RUN_COUNT == 0
            heartbeat_interval = 30.0 if is_first_subprocess_run else 300.0
            next_heartbeat = time.monotonic() + heartbeat_interval
            _COMPUTE_SUBPROCESS_RUN_COUNT += 1
            while True:
                if callable(should_cancel) and should_cancel():
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    try:
                        proc.wait(timeout=2.0)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    raise _TaskCancelledError("Compute cancelled by user.")

                if proc.poll() is not None:
                    break

                now = time.monotonic()
                if now >= next_heartbeat and logf:
                    if is_first_subprocess_run:
                        logf.write(
                            "[INFO] compute subprocess still running (first run can take longer while warming imports and caches).\n"
                        )
                        # After the first warm-up notice, use sparse heartbeat logging.
                        is_first_subprocess_run = False
                        heartbeat_interval = 300.0
                    else:
                        logf.write("[INFO] compute subprocess still running.\n")
                    next_heartbeat = now + heartbeat_interval

                if now > deadline:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    try:
                        proc.wait(timeout=2.0)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    raise RuntimeError(
                        "Compute backend timed out after 15 minutes while processing tiles. "
                        "Please retry with smaller AOI/date range or lower cloud threshold, and check runtime.log/gdal.log."
                    )

                time.sleep(0.2)

            proc.wait(timeout=5)
        finally:
            try:
                stdout_handle.close()
            except Exception:
                pass
            try:
                stderr_handle.close()
            except Exception:
                pass
            if proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass

        try:
            with open(stdout_capture_path, "r", encoding="utf-8", errors="replace") as sf:
                stdout_data = sf.read()
        except Exception:
            stdout_data = ""
        try:
            with open(stderr_capture_path, "r", encoding="utf-8", errors="replace") as ef:
                stderr_data = ef.read()
        except Exception:
            stderr_data = ""

        if logf and stdout_data:
            logf.write("[subprocess_stdout]\n")
            logf.write(stdout_data.strip() + "\n")
        if logf and stderr_data:
            logf.write("[subprocess_stderr]\n")
            logf.write(stderr_data.strip() + "\n")

        if proc.returncode != 0:
            stderr_tail = (stderr_data or "").strip()
            if stderr_tail:
                stderr_tail = stderr_tail[-800:]
            raise RuntimeError(
                f"Engine subprocess failed (exit code {proc.returncode}). "
                f"python={python_exe}. stderr_tail={stderr_tail}"
            )
    finally:
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass


class _VirtughanTask(QgsTask):
    """Runs VirtughanProcessor.compute() off the UI thread and writes to runtime.log."""
    def __init__(self, desc, params, log_path, on_done=None):
        super().__init__(desc, QgsTask.CanCancel)
        self.params = params
        self.log_path = log_path
        self.on_done = on_done
        self.exc = None

    def cancel(self):
        try:
            with open(self.log_path, "a", encoding="utf-8", buffering=1) as logf:
                logf.write("[cancel] Compute cancellation requested by user.\n")
        except Exception:
            pass
        return super().cancel()

    def run(self):
        managed_env_keys = (
            "CPL_LOG",
            "GDAL_HTTP_TIMEOUT",
            "GDAL_HTTP_MAX_RETRY",
            "GDAL_HTTP_RETRY_DELAY",
            "GDAL_HTTP_MULTIRANGE",
            "CPL_VSIL_CURL_USE_HEAD",
            "VSI_CACHE",
            "VSI_CACHE_SIZE",
            "CPL_VSIL_CURL_CACHE_SIZE",
            "CPL_DEBUG",
            "PROJ_LIB",
            "PROJ_DATA",
        )
        original_env = {key: os.environ.get(key) for key in managed_env_keys}
        try:
            os.makedirs(self.params["output_dir"], exist_ok=True)

            os.environ["CPL_LOG"] = os.path.join(self.params["output_dir"], "gdal.log")

            with open(self.log_path, "a", encoding="utf-8", buffering=1) as logf:
                if sys.platform == "darwin":
                    logf.write("[INFO] macOS path: running compute in-process with module reload.\n")
                    _run_engine_inprocess_mac(
                        self.params,
                        logf=logf,
                        should_cancel=self.isCanceled,
                    )
                else:
                    _run_engine_in_subprocess(
                        self.params,
                        self.log_path,
                        logf=logf,
                        should_cancel=self.isCanceled,
                    )
            return True
        except _TaskCancelledError as e:
            self.exc = e
            return False
        except Exception as e:
            self.exc = e
            try:
                with open(self.log_path, "a", encoding="utf-8", buffering=1) as logf:
                    logf.write("[exception]\n")
                    logf.write(traceback.format_exc())
            except Exception:
                pass
            return False
        finally:
            for key, value in original_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def finished(self, ok):
        if self.on_done:
            try:
                self.on_done(ok, self.exc)
            except Exception:
                pass


class _UiLogTailer:
    """Polls a text file and appends new content to a QPlainTextEdit without blocking UI."""
    def __init__(
        self,
        log_path: str,
        log_widget: QPlainTextEdit,
        interval_ms: int = 400,
        on_stall=None,
        stall_timeout_sec: float = 180.0,
    ):
        self._path = log_path
        self._widget = log_widget
        self._pos = 0
        self._on_stall = on_stall
        self._stall_timeout_sec = max(30.0, float(stall_timeout_sec))
        self._last_growth_monotonic = 0.0
        self._stalled_notified = False
        self._timer = QTimer()
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._poll_once)

    def start(self):
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            open(self._path, "a", encoding="utf-8").close()
        except Exception:
            pass
        self._pos = 0
        self._last_growth_monotonic = time.monotonic()
        self._stalled_notified = False
        self._timer.start()

    def stop(self):
        self._timer.stop()

    def _poll_once(self):
        try:
            if not os.path.exists(self._path):
                return
            with open(self._path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self._pos)
                chunk = f.read()
                if chunk:
                    # tqdm uses carriage returns for in-place progress updates.
                    normalized = chunk.replace("\r\n", "\n").replace("\r", "\n")
                    self._widget.appendPlainText(normalized.rstrip("\n"))
                    self._pos = f.tell()
                    self._last_growth_monotonic = time.monotonic()
                    self._stalled_notified = False
                else:
                    idle_sec = time.monotonic() - self._last_growth_monotonic
                    if (not self._stalled_notified) and idle_sec >= self._stall_timeout_sec:
                        self._stalled_notified = True
                        if callable(self._on_stall):
                            try:
                                self._on_stall(idle_sec)
                            except Exception:
                                pass
        except Exception:
            pass


class EngineDockWidget(QDockWidget):
    def __init__(self, iface):
        super().__init__("VirtuGhan • Compute", iface.mainWindow())
        self.iface = iface
        self.setObjectName("VirtuGhanEngineDock")

        self.ui_root = QWidget(self)
        self._form_owner = FORM_CLASS()
        self._form_owner.setupUi(self.ui_root)
        self._set_header_logo()
        self.setWidget(self.ui_root)

        f = self.ui_root.findChild

        self.progressBar        = f(QProgressBar, "progressBar")
        self.runButton          = f(QPushButton,   "runButton")
        self.resetButton        = f(QPushButton,   "resetButton")
        self.helpButton         = f(QPushButton,   "helpButton")
        self.logText            = f(QPlainTextEdit,"logText")

        self.commonHost         = f(QWidget,       "commonParamsContainer")

        self.aoiModeCombo       = f(QComboBox,     "aoiModeCombo")
        self.aoiUseCanvasButton = f(QPushButton,   "aoiUseCanvasButton")
        self.aoiStartDrawButton = f(QPushButton,   "aoiStartDrawButton")  
        self.aoiClearButton     = f(QPushButton,   "aoiClearButton")
        self.aoiPreviewLabel    = f(QLabel,        "aoiPreviewLabel")

        self.opCombo            = f(QComboBox,     "opCombo")
        self.timeseriesCheck    = f(QCheckBox,     "timeseriesCheck")
        self.smartFilterCheck   = f(QCheckBox,     "smartFilterCheck")
        self.workersSpin        = f(QSpinBox,      "workersSpin")
        self.outputPathEdit     = f(QLineEdit,     "outputPathEdit")
        self.outputBrowseButton = f(QPushButton,   "outputBrowseButton")
        self.previewScenesButton = f(QPushButton,  "previewScenesButton")
        self.showSceneFootprintsCheck = f(QCheckBox, "showSceneFootprintsCheck")

        self.simpleModeRadio    = f(QRadioButton,  "simpleModeRadio")
        self.advancedModeRadio  = f(QRadioButton,  "advancedModeRadio")
        self.indexCombo         = f(QComboBox,     "indexCombo")
        self.formulaReferenceLabel = f(QLabel,     "formulaReferenceLabel")
        self.labelAdvancedBand1 = f(QLabel,        "labelAdvancedBand1")
        self.advancedBand1Combo = f(QComboBox,     "advancedBand1Combo")
        self.labelAdvancedBand2 = f(QLabel,        "labelAdvancedBand2")
        self.advancedBand2Combo = f(QComboBox,     "advancedBand2Combo")
        self.labelAdvancedFormula = f(QLabel,      "labelAdvancedFormula")
        self.advancedFormulaEdit = f(QLineEdit,    "advancedFormulaEdit")

        critical = {
            "progressBar": self.progressBar, "runButton": self.runButton,
            "resetButton": self.resetButton, "helpButton": self.helpButton,
            "logText": self.logText, "commonParamsContainer": self.commonHost,
            "aoiModeCombo": self.aoiModeCombo, "aoiUseCanvasButton": self.aoiUseCanvasButton,
            "aoiStartDrawButton": self.aoiStartDrawButton, "aoiClearButton": self.aoiClearButton,
            "aoiPreviewLabel": self.aoiPreviewLabel, "opCombo": self.opCombo,
            "timeseriesCheck": self.timeseriesCheck, "smartFilterCheck": self.smartFilterCheck,
            "workersSpin": self.workersSpin, "outputPathEdit": self.outputPathEdit,
            "outputBrowseButton": self.outputBrowseButton,
            "previewScenesButton": self.previewScenesButton,
            "showSceneFootprintsCheck": self.showSceneFootprintsCheck,
            "simpleModeRadio": self.simpleModeRadio, "advancedModeRadio": self.advancedModeRadio,
            "indexCombo": self.indexCombo, "formulaReferenceLabel": self.formulaReferenceLabel,
            "labelAdvancedBand1": self.labelAdvancedBand1, "advancedBand1Combo": self.advancedBand1Combo,
            "labelAdvancedBand2": self.labelAdvancedBand2, "advancedBand2Combo": self.advancedBand2Combo,
            "labelAdvancedFormula": self.labelAdvancedFormula, "advancedFormulaEdit": self.advancedFormulaEdit,
        }
        missing = [name for name, ref in critical.items() if ref is None]
        if missing:
            raise RuntimeError(
                f"Compute UI missing widgets: {', '.join(missing)}. "
                f"Make sure form field names match the code."
            )

        self._init_common_widget()

        self._init_index_controls()

        self.progressBar.setVisible(False)
        self.workersSpin.setMinimum(1)
        self.workersSpin.setValue(self._recommended_default_workers())

        self._aoi_bbox = None
        # Blue colors for Engine AOI
        self._aoi_fill_color = QColor(0, 102, 255, 60)      # light blue with transparency
        self._aoi_stroke_color = QColor(0, 102, 255, 200)   # darker blue stroke
        self._aoi = AoiManager(self.iface, layer_name="Compute AOI", fill_color=self._aoi_fill_color, stroke_color=self._aoi_stroke_color)
        self._prev_tool = None  # Track previous map tool
        self._drawing_tool = None  # Track active drawing tool

        # Convert dropdown to 4 options at runtime (no .ui change required)
        self.aoiModeCombo.clear()
        self.aoiModeCombo.addItems(["Select mode", "Map extent", "Draw rectangle", "Draw polygon"])

        # Use a single action button; hide the separate 'Use Canvas Extent' button
        self.aoiUseCanvasButton.setVisible(False)

        self.aoiStartDrawButton.clicked.connect(self._aoi_action_clicked)
        self.aoiClearButton.clicked.connect(self._clear_aoi)
        self.aoiModeCombo.currentTextChanged.connect(self._aoi_mode_changed)

        self.outputBrowseButton.clicked.connect(self._browse_output)
        self.resetButton.clicked.connect(self._reset_form)
        self.runButton.clicked.connect(self._run_clicked)
        self.helpButton.clicked.connect(self._open_help)
        self.previewScenesButton.clicked.connect(self._preview_matching_scenes)
        self.showSceneFootprintsCheck.toggled.connect(self._on_show_scene_footprints_toggled)

        # Initialize AOI preview and action button label
        self._update_aoi_preview()
        self._aoi_mode_changed(self.aoiModeCombo.currentText())

        self._tailer = None
        self._current_task = None
        self._current_log_path = None
        self._default_run_button_text = self.runButton.text() or "Compute"
        self._scene_footprints_layer = None
        self._selected_preview_scenes = []
        self._has_successful_run = False
        self._last_output_layer_ids = []

    def _set_header_logo(self):
        try:
            title_label = self.ui_root.findChild(QLabel, "titleLabel")
            if title_label is None:
                return
            if self.ui_root.findChild(QLabel, "virtughanHeaderLogo") is not None:
                return
            header_layout = self.ui_root.findChild(QHBoxLayout, "headerLayout")
            if header_layout is None:
                return
            logo_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "images", "virtughan-logo.png")
            if not os.path.exists(logo_path):
                return

            px = QPixmap(logo_path)
            if px.isNull():
                return

            logo_label = QLabel(self.ui_root)
            logo_label.setObjectName("virtughanHeaderLogo")
            logo_label.setFixedSize(24, 24)
            logo_label.setAlignment(Qt.AlignCenter)
            logo_label.setPixmap(px.scaled(24, 24, Qt.KeepAspectRatio, Qt.SmoothTransformation))

            idx = header_layout.indexOf(title_label)
            header_layout.insertWidget(max(0, idx), logo_label)
            header_layout.setSpacing(6)
        except Exception:
            pass

    def _init_common_widget(self):
        host = self.commonHost
        v = QVBoxLayout(host); v.setContentsMargins(0, 0, 0, 0)

        if CommonParamsWidget is not None:
            self._common = CommonParamsWidget(host)
            try:
                self._common.set_defaults(
                    start_date=QDate.currentDate().addMonths(-1),
                    end_date=QDate.currentDate(),
                    cloud=80,
                    band1="red",
                    band2="nir",
                    formula="(band2-band1)/(band2+band1)",
                )
            except Exception:
                pass
            v.addWidget(self._common)
            _log(self, "Using CommonParamsWidget.", Qgis.Info)
        else:
            fb = QWidget(host)
            form = QFormLayout(fb)
            self.fb_start = QDateEdit(fb); self.fb_start.setCalendarPopup(True); self.fb_start.setDate(QDate.currentDate().addMonths(-1))
            self.fb_end   = QDateEdit(fb); self.fb_end.setCalendarPopup(True);   self.fb_end.setDate(QDate.currentDate())
            self.fb_cloud = QSpinBox(fb);  self.fb_cloud.setRange(0,100); self.fb_cloud.setValue(30)
            self.fb_formula = QLineEdit("(band2-band1)/(band2+band1)", fb)
            self.fb_band1 = QLineEdit("red", fb)
            self.fb_band2 = QLineEdit("nir", fb)
            form.addRow("Start date", self.fb_start)
            form.addRow("End date", self.fb_end)
            form.addRow("Max cloud cover (%)", self.fb_cloud)
            form.addRow("Formula", self.fb_formula)
            form.addRow("Band 1", self.fb_band1)
            form.addRow("Band 2 (optional)", self.fb_band2)
            v.addWidget(fb)
            self._common = None
            _log(self, f"CommonParamsWidget not available: {COMMON_IMPORT_ERROR}", Qgis.Warning)

    def _get_common_params(self):
        band1 = self.advancedBand1Combo.currentText().strip()
        band2 = self.advancedBand2Combo.currentText().strip()
        formula = self.advancedFormulaEdit.text().strip()

        if self._common is not None:
            params = self._common.get_params()
            params["band1"] = band1
            params["band2"] = (band2 or None)
            params["formula"] = formula
            return params
        return {
            "start_date": self.fb_start.date().toString("yyyy-MM-dd"),
            "end_date": self.fb_end.date().toString("yyyy-MM-dd"),
            "cloud_cover": int(self.fb_cloud.value()),
            "band1": band1,
            "band2": (band2 or None),
            "formula": formula,
        }

    def _init_index_controls(self):
        self._index_presets = index_presets_two_band()
        self._index_updating = False

        bands_meta = load_bands_meta()
        populate_band_combos(self.advancedBand1Combo, self.advancedBand2Combo, bands_meta)
        self.advancedBand1Combo.setCurrentText("red")
        self.advancedBand2Combo.setCurrentText("nir")
        self.advancedFormulaEdit.setText("(band2-band1)/(band2+band1)")

        self.indexCombo.clear()
        self.indexCombo.addItems([preset.get("label", "") for preset in self._index_presets])

        self.indexCombo.currentTextChanged.connect(self._on_index_changed)
        self.simpleModeRadio.toggled.connect(self._on_formula_mode_toggled)
        self.advancedModeRadio.toggled.connect(self._on_formula_mode_toggled)

        self.advancedBand1Combo.currentTextChanged.connect(self._sync_reference_from_advanced)
        self.advancedBand2Combo.currentTextChanged.connect(self._sync_reference_from_advanced)
        self.advancedFormulaEdit.textChanged.connect(self._sync_reference_from_advanced)

        current = self._get_common_params()
        matched = match_index_preset(current.get("band1"), current.get("band2"), current.get("formula"))
        if matched:
            self.indexCombo.setCurrentText(matched)
        elif self.indexCombo.count() > 0:
            self.indexCombo.setCurrentIndex(0)

        self._on_formula_mode_toggled()

    def _on_index_changed(self, label):
        if self._index_updating or not self.simpleModeRadio.isChecked():
            return
        self._apply_index_preset(label)

    def _on_formula_mode_toggled(self, *_):
        is_simple = self.simpleModeRadio.isChecked()

        label_index = self.ui_root.findChild(QLabel, "labelIndex")
        label_formula_ref = self.ui_root.findChild(QLabel, "labelFormulaRef")
        if label_index is not None:
            label_index.setVisible(is_simple)
        if label_formula_ref is not None:
            label_formula_ref.setVisible(is_simple)
        self.indexCombo.setVisible(is_simple)
        self.formulaReferenceLabel.setVisible(is_simple)

        self.labelAdvancedBand1.setVisible(not is_simple)
        self.advancedBand1Combo.setVisible(not is_simple)
        self.labelAdvancedBand2.setVisible(not is_simple)
        self.advancedBand2Combo.setVisible(not is_simple)
        self.labelAdvancedFormula.setVisible(not is_simple)
        self.advancedFormulaEdit.setVisible(not is_simple)

        # Show tip in both Simple and Advanced modes
        index_tip = self.ui_root.findChild(QLabel, "indexTipLabel")
        if index_tip is not None:
            index_tip.setVisible(True)

        if self._common is not None:
            for widget_name in ("labelBand1", "band1Combo", "labelBand2", "band2Combo", "labelFormula", "formulaEdit"):
                widget = self._common.findChild(QWidget, widget_name)
                if widget is not None:
                    widget.setVisible(False)

        if is_simple:
            self._apply_index_preset(self.indexCombo.currentText())
        else:
            self._sync_reference_from_advanced()

    def _sync_reference_from_advanced(self, *_):
        band1 = self.advancedBand1Combo.currentText().strip()
        band2 = self.advancedBand2Combo.currentText().strip()
        formula = self.advancedFormulaEdit.text().strip()

        matched = match_index_preset(band1, band2, formula)
        if matched:
            self._index_updating = True
            try:
                self.indexCombo.setCurrentText(matched)
            finally:
                self._index_updating = False

        self.formulaReferenceLabel.setText(self._format_formula_reference(formula, band1, band2))

    def _apply_index_preset(self, label):
        preset = get_index_preset(label)
        if not preset:
            return

        self._index_updating = True
        try:
            self.advancedBand1Combo.setCurrentText(preset.get("band1", ""))
            self.advancedBand2Combo.setCurrentText(preset.get("band2", ""))
            self.advancedFormulaEdit.setText(preset.get("formula", ""))

            self.formulaReferenceLabel.setText(
                self._format_formula_reference(
                    preset.get("formula", ""),
                    preset.get("band1", ""),
                    preset.get("band2", ""),
                )
            )
        finally:
            self._index_updating = False

    def _format_formula_reference(self, formula: str, band1: str, band2: str) -> str:
        formula_text = (formula or "").strip()
        if not formula_text:
            return "(formula will display here)"
        return f"{formula_text}, band1={band1 or '-'}, band2={band2 or '-'}"

    def _aoi_mode_changed(self, text: str):
        """Show AOI action controls only after mode selection and set action text."""
        t = (text or "").lower()
        if "select" in t:
            self.aoiStartDrawButton.setVisible(False)
            self.aoiClearButton.setVisible(False)
            return

        self.aoiStartDrawButton.setVisible(True)
        self.aoiClearButton.setVisible(True)

        if "extent" in t:
            self.aoiStartDrawButton.setText("Use Canvas Extent")
            self.aoiStartDrawButton.setToolTip("Capture current map canvas extent")
        elif "rectangle" in t:
            self.aoiStartDrawButton.setText("Draw Rectangle")
            self.aoiStartDrawButton.setToolTip("Press, drag, release to draw a rectangle")
        else:
            self.aoiStartDrawButton.setText("Draw Polygon")
            self.aoiStartDrawButton.setToolTip("Left-click to add vertices, right-click/Enter/double-click to finish")

    def _aoi_action_clicked(self):
        """Single action button handler; dispatch by dropdown mode."""
        mode = (self.aoiModeCombo.currentText() or "").lower()
        if "select" in mode:
            return
        if "extent" in mode:
            self._use_canvas_extent()
        elif "rectangle" in mode:
            self._start_draw_rectangle()
        else:
            self._start_draw_polygon()

    def _use_canvas_extent(self):
        canvas = self.iface.mapCanvas()
        if not canvas or not canvas.extent():
            QMessageBox.warning(self, "VirtuGhan", "No map canvas extent available.")
            return

        rect = canvas.extent()
        # visible AOI (map CRS)
        rect_geom = QgsGeometry.fromRect(rect)
        self._aoi.replace_geometry(rect_geom)

        # processing bbox (WGS84)
        self._aoi_bbox = rect_to_wgs84_bbox(rect, QgsProject.instance())
        self._update_aoi_preview()

    def _start_draw_rectangle(self):
        """Use common AoiRectTool; press-drag-release to finish."""
        canvas = self.iface.mapCanvas()
        if not canvas:
            QMessageBox.warning(self, "VirtuGhan", "Map canvas not available.")
            return

        self._prev_tool = canvas.mapTool()

        def _finish(rect: QgsRectangle | None):
            # Only restore previous tool if it's not a drawing tool
            from virtughan_qgis.common.aoi import AoiRectTool, AoiPolygonTool
            try:
                if self._prev_tool and not isinstance(self._prev_tool, (AoiRectTool, AoiPolygonTool)):
                    canvas.setMapTool(self._prev_tool)
                else:
                    canvas.setMapTool(None)  # Reset to default tool
            except Exception:
                canvas.setMapTool(None)
            # Restore cursor and message bar
            canvas.setCursor(QCursor(Qt.ArrowCursor))
            self.iface.messageBar().clearWidgets()
            
            if not rect or rect.isEmpty():
                _log(self, "AOI rectangle drawing canceled.")
                return

            # visible AOI (map CRS)
            self._aoi.replace_geometry(QgsGeometry.fromRect(rect))

            # processing bbox (WGS84)
            self._aoi_bbox = rect_to_wgs84_bbox(rect, QgsProject.instance())
            self._update_aoi_preview()

        tool = AoiRectTool(canvas, _finish, stroke_color=self._aoi_stroke_color, fill_color=self._aoi_fill_color)
        self._drawing_tool = tool  # Keep reference for cleanup
        canvas.setMapTool(tool)
        # Show message and change cursor
        canvas.setCursor(QCursor(Qt.CrossCursor))
        self.iface.messageBar().pushInfo(
            "VirtuGhan", 
            "Click and drag on the map to draw a rectangle"
        )
        _log(self, "Draw rectangle: press, drag, release to finish. Esc to cancel.")

    def _start_draw_polygon(self):
        canvas = self.iface.mapCanvas()
        if not canvas:
            QMessageBox.warning(self, "VirtuGhan", "Map canvas not available.")
            return

        self._prev_tool = canvas.mapTool()

        def _done(geom_map: QgsGeometry | None):
            # Only restore previous tool if it's not a drawing tool
            from virtughan_qgis.common.aoi import AoiRectTool, AoiPolygonTool
            try:
                if self._prev_tool and not isinstance(self._prev_tool, (AoiRectTool, AoiPolygonTool)):
                    canvas.setMapTool(self._prev_tool)
                else:
                    canvas.setMapTool(None)  # Reset to default tool
            except Exception:
                canvas.setMapTool(None)
            # Restore cursor and message bar
            canvas.setCursor(QCursor(Qt.ArrowCursor))
            self.iface.messageBar().clearWidgets()

            if geom_map is None or geom_map.isEmpty():
                _log(self, "AOI polygon drawing canceled.")
                return

            # visible AOI (map CRS)
            self._aoi.replace_geometry(geom_map)

            # processing bbox (WGS84)
            self._aoi_bbox = geom_to_wgs84_bbox(geom_map, QgsProject.instance())
            self._update_aoi_preview()

        tool = AoiPolygonTool(canvas, _done, stroke_color=self._aoi_stroke_color, fill_color=self._aoi_fill_color)
        self._drawing_tool = tool  # Keep reference for cleanup
        canvas.setMapTool(tool)
        # Show message and change cursor
        canvas.setCursor(QCursor(Qt.CrossCursor))
        self.iface.messageBar().pushInfo(
            "VirtuGhan",
            "Left-click to add points, right-click or double-click to finish"
        )
        _log(self, "Draw polygon: left-click to add, right-click/Enter/double-click to finish, Esc to cancel.")

    def _clear_aoi(self):
        # Reset drawing tool if active
        canvas = self.iface.mapCanvas()
        if canvas:
            # Clean up any rubber band from the drawing tool
            if hasattr(self, '_drawing_tool') and self._drawing_tool and hasattr(self._drawing_tool, 'rb'):
                try:
                    self._drawing_tool.rb.reset()
                except Exception:
                    pass
            # Restore previous map tool
            if self._prev_tool:
                canvas.setMapTool(self._prev_tool)
            canvas.setCursor(QCursor(Qt.ArrowCursor))
            self.iface.messageBar().clearWidgets()
        
        self._drawing_tool = None  # Clear reference
        self._aoi_bbox = None
        self._update_aoi_preview()
        self._aoi.clear()

    def _update_aoi_preview(self):
        if self._aoi_bbox:
            x1, y1, x2, y2 = self._aoi_bbox
            self.aoiPreviewLabel.setText(f"AOI (EPSG:4326): ({x1:.6f}, {y1:.6f}, {x2:.6f}, {y2:.6f})")
        else:
            self.aoiPreviewLabel.setText("<i>AOI: not set yet. Use smaller aoi and test first.</i>")



    def _open_help(self):
        host = self.window()
        if host and hasattr(host, "show_help_for"):
            host.show_help_for("engine")
            return

    def _browse_output(self):
        path = QFileDialog.getExistingDirectory(self, "Select output folder")
        if path:
            self.outputPathEdit.setText(path)

    def _reset_form(self):
        # Reset common params to defaults
        if self._common is not None:
            try:
                self._common.set_defaults(
                    start_date=QDate.currentDate().addMonths(-1),
                    end_date=QDate.currentDate(),
                    cloud=30,
                    band1="red",
                    band2="nir",
                    formula="(band2-band1)/(band2+band1)",
                )
            except Exception:
                pass
        else:
            try:
                self.fb_start.setDate(QDate.currentDate().addMonths(-1))
                self.fb_end.setDate(QDate.currentDate())
                self.fb_cloud.setValue(30)
                self.fb_formula.setText("(band2-band1)/(band2+band1)")
                self.fb_band1.setText("red")
                self.fb_band2.setText("nir")
            except Exception:
                pass

        # Reset AOI + UI 
        self._aoi_bbox = None
        self._update_aoi_preview()
        self._aoi.clear()

        self.opCombo.setCurrentIndex(7)
        self.timeseriesCheck.setChecked(False)
        self.smartFilterCheck.setChecked(False)
        self.workersSpin.setMinimum(1)
        self.workersSpin.setValue(self._recommended_default_workers())
        self.outputPathEdit.clear()
        self.logText.clear()

        self.advancedBand1Combo.setCurrentText("red")
        self.advancedBand2Combo.setCurrentText("nir")
        self.advancedFormulaEdit.setText("(band2-band1)/(band2+band1)")
        self.simpleModeRadio.setChecked(True)
        self._on_formula_mode_toggled()

  
    # Collect params / run task
    def _recommended_default_workers(self) -> int:
        cpu_count = os.cpu_count() or 1
        return 2 if cpu_count <= 2 else 4

    def _effective_workers(self) -> int:
        return max(1, int(self.workersSpin.value()))

    def _collect_params(self):
        if not self._aoi_bbox:
            raise RuntimeError("Please set AOI (Map extent / Draw rectangle / Draw polygon) before running.")

        # quick check for WGS84-like bbox
        b = self._aoi_bbox
        if not (len(b) == 4 and -180 <= b[0] < b[2] <= 180 and -90 <= b[1] < b[3] <= 90):
            raise RuntimeError(f"AOI bbox does not look like EPSG:4326: {self._aoi_bbox}")

        p = self._get_common_params()

        # In Simple mode, the dropdown selection is authoritative at run time.
        # This prevents stale advanced-field values from leaking into compute params.
        if self.simpleModeRadio.isChecked():
            preset = get_index_preset(self.indexCombo.currentText())
            if not preset:
                raise RuntimeError("Please select a valid index preset.")
            p["band1"] = (preset.get("band1") or "").strip()
            p["band2"] = (preset.get("band2") or "").strip() or None
            p["formula"] = (preset.get("formula") or "").strip()

        sdt = QDate.fromString(p["start_date"], "yyyy-MM-dd")
        edt = QDate.fromString(p["end_date"], "yyyy-MM-dd")
        if not sdt.isValid() or not edt.isValid():
            raise RuntimeError("Please pick valid start/end dates.")
        if sdt > edt:
            raise RuntimeError("Start date must be before end date.")
        if not p.get("formula"):
            raise RuntimeError("Formula is required.")
        if not p.get("band1"):
            raise RuntimeError("Band 1 is required.")
        if self.advancedModeRadio.isChecked() and not p.get("band2"):
            raise RuntimeError("Band 2 is required in Advanced mode.")

        op_txt = (self.opCombo.currentText() or "").strip()
        operation = None if op_txt == "none" else op_txt

        if (not self.timeseriesCheck.isChecked()) and (operation is None):
            raise RuntimeError("Operation is required when 'Generate timeseries' is disabled.")

        workers = self._effective_workers()
        out_base = (self.outputPathEdit.text() or "").strip() or QgsProcessingUtils.tempFolder()
        out_dir = os.path.join(out_base, f"virtughan_compute_{uuid.uuid4().hex[:8]}")

        return dict(
            bbox=self._aoi_bbox,
            start_date=p["start_date"],
            end_date=p["end_date"],
            cloud_cover=int(p["cloud_cover"]),
            formula=p["formula"],
            band1=p["band1"],
            band2=p.get("band2") or None,
            operation=operation,
            timeseries=self.timeseriesCheck.isChecked(),
            smart_filter=self.smartFilterCheck.isChecked(),
            workers=workers,
            output_dir=out_dir,
        )

    def _start_tailing(self, log_path: str):
        self._current_log_path = log_path
        self._tailer = _UiLogTailer(
            log_path,
            self.logText,
            interval_ms=400,
        )
        self._tailer.start()

    def _stop_tailing(self):
        if self._tailer:
            self._tailer.stop()
            self._tailer = None
        self._current_log_path = None

    def _run_clicked(self):
        try:
            if self._current_task is not None:
                status = self._current_task.status()
                if status in (QgsTask.Queued, QgsTask.Running):
                    self._cancel_current_run()
                    return
        except Exception:
            pass

        if not ensure_runtime_network_ready(self):
            _log(self, "Runtime network preflight failed; run cancelled.", Qgis.Warning)
            return

        try:
            _log(self, "Pre-download stage started: checking matching scenes before compute...")
            self._validate_minimum_matching_scenes(min_count=2, timeout_s=120.0)
            _log(self, "Pre-download stage completed: scene check passed.")
            params = self._collect_params()
        except TimeoutError:
            _log(self, "Pre-compute scene check timed out after 2 minutes.", Qgis.Warning)
            QMessageBox.warning(
                self,
                "VirtuGhan",
                "Pre-download stage timed out after 2 minutes before compute started.\n\n"
                "Please try again later.",
            )
            return
        except Exception as e:
            QMessageBox.warning(self, "VirtuGhan", str(e))
            return

        mode_label = "Simple" if self.simpleModeRadio.isChecked() else "Advanced"
        preset_label = (self.indexCombo.currentText() or "").strip() if self.simpleModeRadio.isChecked() else "(custom)"
        _log(
            self,
            "Index selection: "
            f"mode={mode_label}, "
            f"preset={preset_label}, "
            f"formula={params.get('formula')}, "
            f"band1={params.get('band1')}, "
            f"band2={params.get('band2') or '-'}",
        )

        out_dir = params["output_dir"]
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception as e:
            QMessageBox.critical(self, "VirtuGhan", f"Cannot create output folder:\n{out_dir}\n\n{e}")
            return

        log_path = os.path.join(out_dir, "runtime.log")
        _log(self, f"Output: {out_dir}")
        _log(self, f"Log file: {log_path}")

        try:
            open(log_path, "a", encoding="utf-8").close()
        except Exception:
            pass

        self._set_running(True)
        self._start_tailing(log_path)
        self._focus_log_section()
        self._has_successful_run = False
        self._last_output_layer_ids = []

        def _on_done(ok, exc):
            self._stop_tailing()
            self._set_running(False)
            task_was_cancelled = False
            try:
                task_was_cancelled = bool(self._current_task and self._current_task.isCanceled())
            except Exception:
                task_was_cancelled = False
            self._current_task = None
            if isinstance(exc, _TaskCancelledError) or task_was_cancelled:
                _log(self, "Compute cancelled by user.", Qgis.Warning)
                QMessageBox.information(self, "VirtuGhan", "Compute cancelled.")
                return
            if not ok or exc:
                _log(self, f"Compute failed: {exc}", Qgis.Critical)
                user_msg = _build_engine_failure_message(exc, log_path=log_path)
                QMessageBox.critical(self, "VirtuGhan", user_msg)
            else:
                extract_zipfiles(out_dir, logger=lambda m, lvl=Qgis.Info: _log(self, m, lvl), delete_archives=True)
                
                added = 0
                loaded_layer_ids = []
                for root, _dirs, files in os.walk(out_dir):
                    for fn in files:
                        if fn.lower().endswith((".tif", ".tiff", ".vrt")):
                            path = os.path.join(root, fn)
                            lyr = QgsRasterLayer(path, os.path.splitext(fn)[0], "gdal")
                            if lyr.isValid():
                                QgsProject.instance().addMapLayer(lyr)
                                loaded_layer_ids.append(lyr.id())
                                _log(self, f"Loaded raster: {path}")
                                added += 1
                            else:
                                _log(self, f"Failed to load raster: {path}", Qgis.Warning)
                if added == 0:
                    _log(self, "No .tif/.tiff/.vrt files found to load.")
                self._last_output_layer_ids = loaded_layer_ids

                self._has_successful_run = True
                if self.showSceneFootprintsCheck.isChecked():
                    scenes_for_map = self._resolve_scenes_for_main_map_footprints()
                    count = self._render_scene_footprints(scenes_for_map, below_layer_ids=self._last_output_layer_ids)
                    if count > 0:
                        _log(self, f"Scene footprints layer added to main map after run completion ({count} scenes).")
                    else:
                        _log(self, "No scene footprints were added (no scenes found for current AOI/date/cloud filters).", Qgis.Warning)
                else:
                    self._clear_scene_footprints_layer()

                results_summary = None
                host = self.window()
                if host and hasattr(host, "show_results_for_output"):
                    try:
                        run_metadata = {
                            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "start_date": params.get("start_date"),
                            "end_date": params.get("end_date"),
                            "cloud_cover": params.get("cloud_cover"),
                            "band1": params.get("band1"),
                            "band2": params.get("band2"),
                            "formula": params.get("formula"),
                            "operation": params.get("operation"),
                            "timeseries": params.get("timeseries"),
                            "smart_filter": params.get("smart_filter"),
                            "workers": params.get("workers"),
                            "bbox": params.get("bbox"),
                        }
                        results_summary = host.show_results_for_output(
                            out_dir,
                            auto_open=True,
                            run_metadata=run_metadata,
                        )
                    except Exception as e:
                        _log(self, f"Could not update Results tab: {e}", Qgis.Warning)

                msg = f"Compute finished.\nOutput: {out_dir}"
                if self.timeseriesCheck.isChecked():
                    frames = 0
                    if isinstance(results_summary, dict):
                        frames = int(results_summary.get("timeseries_frames", 0) or 0)
                    if frames > 0:
                        msg += f"\n\nTimeseries outputs are available in the Results tab ({frames} frame(s))."
                    else:
                        msg += "\n\nTimeseries was requested, but no timeseries image frames were found in output."

                QMessageBox.information(self, "VirtuGhan", msg)

        self._current_task = _VirtughanTask("VirtuGhan Compute", params, log_path, on_done=_on_done)
        _log(self, "Compute handoff: backend task queued.")
        QgsApplication.taskManager().addTask(self._current_task)

    def _focus_log_section(self):
        try:
            self.logText.setFocus(Qt.OtherFocusReason)
            sb = self.logText.verticalScrollBar()
            sb.setValue(sb.maximum())
        except Exception:
            pass

        try:
            parent = self.logText.parentWidget()
            while parent is not None:
                if isinstance(parent, QScrollArea):
                    parent.ensureWidgetVisible(self.logText, 0, 24)
                    break
                parent = parent.parentWidget()
        except Exception:
            pass

    def _set_running(self, running: bool):
        self.progressBar.setVisible(running)
        self.progressBar.setRange(0, 0 if running else 1)
        self.runButton.setEnabled(True)
        self.runButton.setText("Cancel" if running else self._default_run_button_text)
        self.resetButton.setEnabled(not running)
        try:
            host = self.window()
            if host and hasattr(host, "set_tab_busy"):
                host.set_tab_busy("engine", running)
        except Exception:
            pass
        for w in (self.aoiStartDrawButton, self.aoiClearButton,
                  self.aoiModeCombo, self.outputBrowseButton,
                  self.showSceneFootprintsCheck):
            try:
                w.setEnabled(not running)
            except Exception:
                pass
        try:
            self.previewScenesButton.setEnabled(True)
        except Exception:
            pass

    def _cancel_current_run(self):
        task = self._current_task
        if task is None:
            return
        try:
            status = task.status()
            if status in (QgsTask.Queued, QgsTask.Running):
                task.cancel()
                _log(self, "Cancellation requested...", Qgis.Warning)
        except Exception as e:
            _log(self, f"Failed to cancel compute task: {e}", Qgis.Warning)

    def _on_show_scene_footprints_toggled(self, checked: bool):
        if not checked:
            self._clear_scene_footprints_layer()
            return
        if self._has_successful_run:
            scenes_for_map = self._resolve_scenes_for_main_map_footprints()
            count = self._render_scene_footprints(scenes_for_map, below_layer_ids=self._last_output_layer_ids)
            if count > 0:
                _log(self, f"Scene footprints layer added to main map ({count} scenes).")
            else:
                _log(self, "No scene footprints were added (no scenes found for current AOI/date/cloud filters).", Qgis.Warning)
        else:
            _log(self, "Scene footprints will be added after a successful run.")

    def _resolve_scenes_for_main_map_footprints(self):
        scenes = list(self._selected_preview_scenes or [])
        if scenes:
            return scenes

        search_fn = _resolve_engine_search_stac_api()
        if search_fn is None:
            _log(self, "Cannot load scene footprints: search_stac_api is not available.", Qgis.Warning)
            return []

        try:
            params = self._collect_search_params()
            scenes = search_fn(
                params["bbox"],
                params["start_date"],
                params["end_date"],
                params["cloud_cover"],
            )
            self._selected_preview_scenes = scenes or []
            _log(self, f"Loaded {len(self._selected_preview_scenes)} scenes from current filters for main-map footprints.")
        except Exception as e:
            _log(self, f"Failed to fetch scenes for main-map footprints: {e}", Qgis.Warning)
            return []

        return list(self._selected_preview_scenes)

    def _clear_scene_footprints_layer(self):
        if self._scene_footprints_layer and self._scene_footprints_layer.isValid():
            try:
                QgsProject.instance().removeMapLayer(self._scene_footprints_layer.id())
            except Exception:
                pass
        self._scene_footprints_layer = None
        try:
            self.iface.mapCanvas().refresh()
        except Exception:
            pass

    def _stac_geometry_to_qgs(self, geom_obj):
        if not isinstance(geom_obj, dict):
            return None
        gtype = geom_obj.get("type")
        coords = geom_obj.get("coordinates")
        if not coords:
            return None
        try:
            if gtype == "Polygon":
                rings = []
                for ring in coords:
                    rings.append([QgsPointXY(float(x), float(y)) for x, y in ring])
                return QgsGeometry.fromPolygonXY(rings)
            if gtype == "MultiPolygon":
                polys = []
                for poly in coords:
                    rings = []
                    for ring in poly:
                        rings.append([QgsPointXY(float(x), float(y)) for x, y in ring])
                    polys.append(rings)
                return QgsGeometry.fromMultiPolygonXY(polys)
        except Exception:
            return None
        return None

    def _collect_search_params(self):
        if not self._aoi_bbox:
            raise RuntimeError("Set AOI first (Map extent / Draw rectangle / Draw polygon).")
        p = self._get_common_params()
        if not p.get("start_date") or not p.get("end_date"):
            raise RuntimeError("Start and End dates are required.")
        return {
            "bbox": self._aoi_bbox,
            "start_date": p["start_date"],
            "end_date": p["end_date"],
            "cloud_cover": int(p.get("cloud_cover", 30)),
        }

    def _search_scenes_with_timeout(
        self,
        bbox,
        start_date,
        end_date,
        cloud_cover,
        timeout_s: float = 30.0,
    ):
        search_fn = _resolve_engine_search_stac_api()
        if search_fn is None:
            raise RuntimeError("search_stac_api is not available in the compute backend.")

        result = {"scenes": None, "error": None}

        def _worker():
            try:
                result["scenes"] = search_fn(
                    bbox,
                    start_date,
                    end_date,
                    cloud_cover,
                )
            except Exception as exc:
                result["error"] = exc

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

        deadline = time.time() + max(1.0, float(timeout_s))
        started_at = time.time()
        next_progress_log_at = started_at + 15.0
        while t.is_alive() and time.time() < deadline:
            now = time.time()
            if now >= next_progress_log_at:
                elapsed = int(now - started_at)
                remaining = max(0, int(deadline - now))
                _log(
                    self,
                    f"Pre-download scene check still running ({elapsed}s elapsed, {remaining}s remaining)...",
                    Qgis.Info,
                )
                next_progress_log_at = now + 15.0
            try:
                QgsApplication.processEvents()
            except Exception:
                pass
            time.sleep(0.05)

        if t.is_alive():
            raise TimeoutError(
                f"Scene search timed out after {int(max(1.0, float(timeout_s)))}s. "
                "Please check internet/VPN/proxy and try again."
            )
        if result["error"] is not None:
            raise result["error"]
        return list(result["scenes"] or [])

    def _validate_minimum_matching_scenes(self, min_count: int = 2, timeout_s: float = 30.0):
        if _resolve_engine_search_stac_api() is None:
            _log(self, "Pre-compute STAC validation skipped because in-process resolver is unavailable.", Qgis.Warning)
            return
        params = self._collect_search_params()
        scenes = self._search_scenes_with_timeout(
            params["bbox"],
            params["start_date"],
            params["end_date"],
            params["cloud_cover"],
            timeout_s=timeout_s,
        )
        count = len(scenes or [])
        if count < min_count:
            raise RuntimeError(
                "Compute requires at least 2 matching scenes for the selected filters. "
                f"Found {count} scene(s). "
                "Try expanding the date range, increasing cloud threshold, or enabling Smart Filter."
            )

    def _render_scene_footprints(self, scenes, below_layer_ids=None):
        self._clear_scene_footprints_layer()
        if not scenes:
            return 0
        project = QgsProject.instance()
        dst_crs = project.crs()
        layer = QgsVectorLayer(f"Polygon?crs={dst_crs.authid()}", "Compute Scene Footprints", "memory")
        prov = layer.dataProvider()
        prov.addAttributes([
            QgsField("scene_id", QVariant.String),
            QgsField("datetime", QVariant.String),
            QgsField("cloud", QVariant.Double),
            QgsField("collection", QVariant.String),
        ])
        layer.updateFields()

        xform = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"), dst_crs, project)
        feats = []
        for scene in scenes:
            geom = self._stac_geometry_to_qgs(scene.get("geometry"))
            if geom is None or geom.isEmpty():
                continue
            try:
                geom.transform(xform)
            except Exception:
                continue
            props = scene.get("properties", {}) or {}
            feat = QgsFeature(layer.fields())
            feat.setGeometry(geom)
            feat.setAttributes([
                str(scene.get("id", "")),
                str(props.get("datetime", "")),
                float(props.get("eo:cloud_cover", -1) or -1),
                str(scene.get("collection", "")),
            ])
            feats.append(feat)

        if not feats:
            _log(self, "No valid scene footprint geometries found.", Qgis.Warning)
            return 0

        prov.addFeatures(feats)
        layer.updateExtents()
        try:
            sym = layer.renderer().symbol()
            sym.setColor(QColor(156, 39, 176, 10))
            sym.symbolLayer(0).setStrokeColor(QColor(156, 39, 176, 120))
            sym.symbolLayer(0).setStrokeWidth(0.35)
            layer.triggerRepaint()
            layer.emitStyleChanged()
        except Exception:
            pass

        project = QgsProject.instance()
        project.addMapLayer(layer, False)
        root = project.layerTreeRoot()
        insert_idx = len(root.children())
        if below_layer_ids:
            ref_indices = []
            for ref_id in below_layer_ids:
                ref_node = root.findLayer(ref_id)
                if ref_node is None:
                    continue
                try:
                    ref_indices.append(root.children().index(ref_node))
                except ValueError:
                    pass
            if ref_indices:
                insert_idx = max(ref_indices) + 1
        root.insertLayer(insert_idx, layer)
        self._scene_footprints_layer = layer
        try:
            self.iface.mapCanvas().refresh()
        except Exception:
            pass
        return len(feats)

    def _preview_matching_scenes(self):
        if _resolve_engine_search_stac_api() is None:
            QMessageBox.warning(self, "VirtuGhan", "search_stac_api is not available in the compute backend.")
            return
        try:
            params = self._collect_search_params()
        except Exception as e:
            QMessageBox.warning(self, "VirtuGhan", str(e))
            return

        self.progressBar.setVisible(True)
        self.progressBar.setRange(0, 0)
        original_btn_text = self.previewScenesButton.text()
        self.previewScenesButton.setText("Loading Preview…")
        self.previewScenesButton.setEnabled(False)
        QgsApplication.processEvents()
        _log(self, "Searching matching scenes...")
        try:
            scenes = self._search_scenes_with_timeout(
                params["bbox"],
                params["start_date"],
                params["end_date"],
                params["cloud_cover"],
                timeout_s=45.0,
            )
            _log(self, f"Matching scenes found: {len(scenes)}")

            aoi_geom, aoi_crs_authid = self._get_current_aoi_geometry_for_preview()

            dlg = ScenePreviewDialog(
                parent=self,
                scenes=scenes,
                title="Compute Scene Preview",
                fill_color=QColor(156, 39, 176, 14),
                stroke_color=QColor(156, 39, 176, 170),
                aoi_geometry=aoi_geom,
                aoi_crs_authid=aoi_crs_authid,
                aoi_fill_color=self._aoi_fill_color,
                aoi_stroke_color=self._aoi_stroke_color,
            )
            dlg.exec_()

            selected_scenes = dlg.selected_scenes()
            self._selected_preview_scenes = selected_scenes
            _log(self, f"Selected scenes in preview: {len(selected_scenes)}")
            _log(self, "Preview is temporary; main map footprints update only after run completion.")
        except Exception as e:
            _log(self, f"Scene search failed: {e}", Qgis.Critical)
            QMessageBox.critical(self, "VirtuGhan", f"Scene search failed:\n{e}")
        finally:
            self.progressBar.setVisible(False)
            self.progressBar.setRange(0, 1)
            self.previewScenesButton.setText(original_btn_text)
            self.previewScenesButton.setEnabled(True)

    def _get_current_aoi_geometry_for_preview(self):
        try:
            layer = getattr(self._aoi, "layer", None)
            if not layer or not layer.isValid():
                return None, None

            feat = None
            for candidate in layer.getFeatures():
                feat = candidate
                break
            if feat is None:
                return None, None

            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                return None, None

            return QgsGeometry(geom), layer.crs().authid()
        except Exception:
            return None, None

    def _teardown_runtime_state(self):
        try:
            self._stop_tailing()
        except Exception:
            pass

        try:
            if self._current_task is not None:
                try:
                    self._current_task.on_done = None
                except Exception:
                    pass
                try:
                    self._current_task.cancel()
                except Exception:
                    pass
        except Exception:
            pass
        self._current_task = None

        try:
            self._clear_scene_footprints_layer()
        except Exception:
            pass

        try:
            self._clear_aoi()
        except Exception:
            pass

        try:
            canvas = self.iface.mapCanvas()
            if canvas is not None:
                from virtughan_qgis.common.aoi import AoiRectTool, AoiPolygonTool

                active_tool = canvas.mapTool()
                if isinstance(active_tool, (AoiRectTool, AoiPolygonTool)):
                    canvas.setMapTool(None)
                canvas.setCursor(QCursor(Qt.ArrowCursor))
            self.iface.messageBar().clearWidgets()
        except Exception:
            pass

        self._drawing_tool = None
        self._prev_tool = None

    def closeEvent(self, event):
        self._teardown_runtime_state()
        super().closeEvent(event)
