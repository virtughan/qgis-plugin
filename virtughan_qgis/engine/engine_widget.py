# virtughan_qgis/engine/engine_widget.py
import os
import sys
import time
import uuid
import traceback
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
from ..bootstrap import ensure_runtime_network_ready

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

VIRTUGHAN_IMPORT_ERROR = None
VirtughanProcessor = None
try:
    from virtughan.engine import VirtughanProcessor, search_stac_api as engine_search_stac_api
except Exception as _e:
    VIRTUGHAN_IMPORT_ERROR = _e
    VirtughanProcessor = None
    engine_search_stac_api = None

UI_PATH = os.path.join(os.path.dirname(__file__), "engine_form.ui")
FORM_CLASS, _ = uic.loadUiType(UI_PATH)


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

    return f"Compute failed:\n{exc}\n\nSee runtime.log for details."


def _resolve_embedded_python_executable() -> str:
    candidates: list[Path] = []
    env_python = os.environ.get("PYTHONEXECUTABLE")
    if env_python:
        candidates.append(Path(env_python))

    prefix = QgsApplication.prefixPath() or ""
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

    if sys.prefix:
        candidates.extend(
            [
                Path(sys.prefix) / "python.exe",
                Path(sys.prefix) / "bin" / "python.exe",
                Path(sys.prefix) / "bin" / "python3",
                Path(sys.prefix) / "bin" / "python",
                Path(sys.prefix) / "python3",
                Path(sys.prefix) / "python",
            ]
        )

    if sys.platform == "darwin" and sys.executable:
        exe_path = Path(sys.executable).resolve()
        for parent in exe_path.parents:
            if parent.name == "MacOS":
                app_contents = parent.parent
                candidates.extend(
                    [
                        parent / "bin" / "python3",
                        app_contents / "MacOS" / "bin" / "python3",
                        app_contents / "Frameworks" / "bin" / "python3",
                        app_contents / "Frameworks" / "Python.framework" / "Versions" / "Current" / "bin" / "python3",
                        app_contents / "Resources" / "python" / "bin" / "python3",
                    ]
                )
                try:
                    fw_versions = app_contents / "Frameworks" / "Python.framework" / "Versions"
                    if fw_versions.is_dir():
                        for version_dir in sorted(fw_versions.iterdir()):
                            candidates.append(version_dir / "bin" / "python3")
                except Exception:
                    pass
                break

    if sys.executable:
        candidates.append(Path(sys.executable))

    seen: set[str] = set()
    for candidate in candidates:
        normalized = str(candidate)
        if normalized in seen:
            continue
        seen.add(normalized)

        name = candidate.name.lower()
        if sys.platform == "darwin" and name == "qgis":
            continue
        if candidate.is_file() and "python" in name:
            return normalized

    if sys.executable and "python" in Path(sys.executable).name.lower():
        return sys.executable
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


class _VirtughanTask(QgsTask):
    """Runs VirtughanProcessor.compute() off the UI thread and writes to runtime.log."""
    def __init__(self, desc, params, log_path, on_done=None):
        super().__init__(desc, QgsTask.CanCancel)
        self.params = params
        self.log_path = log_path
        self.on_done = on_done
        self.exc = None

    def run(self):
        try:
            os.makedirs(self.params["output_dir"], exist_ok=True)

            os.environ["CPL_LOG"] = self.log_path

            with open(self.log_path, "a", encoding="utf-8", buffering=1) as logf:
                _configure_geospatial_runtime(logf=logf)
                logf.write(f"[{datetime.now().isoformat(timespec='seconds')}] Starting VirtughanProcessor\n")
                logf.write(f"Params: {self.params}\n")
                try:
                    import rasterio

                    logf.write(f"rasterio version: {getattr(rasterio, '__version__', 'unknown')}\n")
                    logf.write(f"rasterio path: {getattr(rasterio, '__file__', 'unknown')}\n")
                    logf.write(
                        f"rasterio GDAL version: {getattr(rasterio, '__gdal_version__', 'unknown')}\n"
                    )
                except Exception as diag_exc:
                    logf.write(f"rasterio diagnostics unavailable: {diag_exc}\n")
                logf.write(f"PROJ_LIB: {os.environ.get('PROJ_LIB', '')}\n")
                logf.write(f"PROJ_DATA: {os.environ.get('PROJ_DATA', '')}\n")
                logf.write(f"sys.executable: {sys.executable}\n")

                max_attempts = 4
                fallback_applied = False
                for attempt in range(1, max_attempts + 1):
                    try:
                        logf.write(f"Compute attempt {attempt}/{max_attempts}\n")
                        proc = VirtughanProcessor(
                            bbox=self.params["bbox"],
                            start_date=self.params["start_date"],
                            end_date=self.params["end_date"],
                            cloud_cover=self.params["cloud_cover"],
                            formula=self.params["formula"],
                            band1=self.params["band1"],
                            band2=self.params["band2"],
                            operation=self.params["operation"],
                            timeseries=self.params["timeseries"],
                            output_dir=self.params["output_dir"],
                            log_file=logf,
                            cmap="RdYlGn",
                            workers=self.params["workers"],
                            smart_filter=self.params["smart_filter"],
                        )
                        with _with_embedded_python_executable(logf=logf):
                            proc.compute()
                        break
                    except Exception as compute_exc:
                        message = str(compute_exc)
                        details = traceback.format_exc()
                        transient_read_error = (
                            "TIFFReadEncodedTile" in message
                            or "IReadBlock failed" in message
                            or "RasterioIOError" in message
                            or "Read failed" in message
                            or "TIFFFillTile" in message
                            or "CPLE_AppDefinedError" in message
                            or "TIFFReadEncodedTile" in details
                            or "IReadBlock failed" in details
                            or "RasterioIOError" in details
                            or "Read failed" in details
                            or "TIFFFillTile" in details
                            or "CPLE_AppDefinedError" in details
                        )
                        if attempt < max_attempts and transient_read_error:
                            if not fallback_applied:
                                _apply_transient_read_fallback(logf=logf)
                                fallback_applied = True
                            wait_s = 2 * attempt
                            logf.write(
                                f"Transient raster read error detected (attempt {attempt}/{max_attempts}); retrying in {wait_s}s...\n"
                            )
                            time.sleep(wait_s)
                            continue
                        raise
                logf.write("compute() finished.\n")
            return True
        except Exception as e:
            self.exc = e
            try:
                with open(self.log_path, "a", encoding="utf-8", buffering=1) as logf:
                    logf.write("[exception]\n")
                    logf.write(traceback.format_exc())
            except Exception:
                pass
            return False

    def finished(self, ok):
        if self.on_done:
            try:
                self.on_done(ok, self.exc)
            except Exception:
                pass


class _UiLogTailer:
    """Polls a text file and appends new content to a QPlainTextEdit without blocking UI."""
    def __init__(self, log_path: str, log_widget: QPlainTextEdit, interval_ms: int = 400):
        self._path = log_path
        self._widget = log_widget
        self._pos = 0
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
                    self._widget.appendPlainText(chunk.rstrip("\n"))
                    self._pos = f.tell()
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
        if self.workersSpin.value() < 1:
            self.workersSpin.setValue(1)

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

        self.formulaReferenceLabel.setText(formula or "(formula will display here)")

    def _apply_index_preset(self, label):
        preset = get_index_preset(label)
        if not preset:
            return

        self._index_updating = True
        try:
            self.advancedBand1Combo.setCurrentText(preset.get("band1", ""))
            self.advancedBand2Combo.setCurrentText(preset.get("band2", ""))
            self.advancedFormulaEdit.setText(preset.get("formula", ""))

            self.formulaReferenceLabel.setText(preset.get("formula", ""))
        finally:
            self._index_updating = False

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
            self.aoiPreviewLabel.setText("<i>AOI: not set yet</i>")



    def _open_help(self):
        host = self.window()
        if host and hasattr(host, "show_help_for"):
            host.show_help_for("engine")
            return

        QMessageBox.information(
            self,
            "VirtuGhan Compute Help",
            "Compute creates analysis rasters from Sentinel-2 imagery.\n\n"
            "Required fields: Start date, End date, Max cloud (%), Band 1, Formula, and AOI.\n"
            "Band 2 is optional.\n\n"
            "You can also use Aggregation, Generate timeseries, smart filter, workers, and scene preview options before running.",
        )

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
        if self.workersSpin.value() < 1:
            self.workersSpin.setValue(1)
        self.outputPathEdit.clear()
        self.logText.clear()

        self.advancedBand1Combo.setCurrentText("red")
        self.advancedBand2Combo.setCurrentText("nir")
        self.advancedFormulaEdit.setText("(band2-band1)/(band2+band1)")
        self.simpleModeRadio.setChecked(True)
        self._on_formula_mode_toggled()

  
    # Collect params / run task
    def _collect_params(self):
        if VirtughanProcessor is None:
            raise RuntimeError(f"VirtughanProcessor import failed: {VIRTUGHAN_IMPORT_ERROR}")
        if not self._aoi_bbox:
            raise RuntimeError("Please set AOI (Map extent / Draw rectangle / Draw polygon) before running.")

        # quick check for WGS84-like bbox
        b = self._aoi_bbox
        if not (len(b) == 4 and -180 <= b[0] < b[2] <= 180 and -90 <= b[1] < b[3] <= 90):
            raise RuntimeError(f"AOI bbox does not look like EPSG:4326: {self._aoi_bbox}")

        p = self._get_common_params()

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

        workers = max(1, int(self.workersSpin.value()))
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
        self._tailer = _UiLogTailer(log_path, self.logText, interval_ms=400)
        self._tailer.start()

    def _stop_tailing(self):
        if self._tailer:
            self._tailer.stop()
            self._tailer = None
        self._current_log_path = None

    def _run_clicked(self):
        if not ensure_runtime_network_ready(self):
            _log(self, "Runtime network preflight failed; run cancelled.", Qgis.Warning)
            return

        try:
            params = self._collect_params()
        except Exception as e:
            QMessageBox.warning(self, "VirtuGhan", str(e))
            return

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
        self.runButton.setEnabled(not running)
        self.resetButton.setEnabled(not running)
        try:
            host = self.window()
            if host and hasattr(host, "set_tab_busy"):
                host.set_tab_busy("engine", running)
        except Exception:
            pass
        for w in (self.aoiStartDrawButton, self.aoiClearButton,
                  self.aoiModeCombo, self.outputBrowseButton,
                  self.previewScenesButton, self.showSceneFootprintsCheck):
            try:
                w.setEnabled(not running)
            except Exception:
                pass

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

        if engine_search_stac_api is None:
            _log(self, "Cannot load scene footprints: search_stac_api is not available.", Qgis.Warning)
            return []

        try:
            params = self._collect_search_params()
            scenes = engine_search_stac_api(
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
        if engine_search_stac_api is None:
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
            scenes = engine_search_stac_api(
                params["bbox"],
                params["start_date"],
                params["end_date"],
                params["cloud_cover"],
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
