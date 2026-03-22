# virtughan_qgis/engine/engine_widget.py
import os
import uuid
import traceback
from datetime import datetime

from qgis.PyQt import uic
from qgis.PyQt.QtCore import Qt, QDate, QTimer, QVariant
from qgis.PyQt.QtGui import QColor, QCursor
from qgis.PyQt.QtWidgets import (
    QWidget, QDockWidget, QFileDialog, QMessageBox,
    QProgressBar, QPlainTextEdit, QComboBox, QCheckBox, QLabel,
    QPushButton, QSpinBox, QLineEdit, QDateEdit, QFormLayout, QVBoxLayout
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

from ..common.map_setup import setup_default_map

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

            os.environ.setdefault("GDAL_HTTP_TIMEOUT", "30")
            os.environ.setdefault("CPL_DEBUG", "ON")
            os.environ["CPL_LOG"] = self.log_path

            with open(self.log_path, "a", encoding="utf-8", buffering=1) as logf:
                logf.write(f"[{datetime.now().isoformat(timespec='seconds')}] Starting VirtughanProcessor\n")
                logf.write(f"Params: {self.params}\n")

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
                proc.compute()
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
        super().__init__("VirtuGhan • Engine", iface.mainWindow())
        self.iface = iface
        self.setObjectName("VirtuGhanEngineDock")

        self.ui_root = QWidget(self)
        self._form_owner = FORM_CLASS()
        self._form_owner.setupUi(self.ui_root)
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
        }
        missing = [name for name, ref in critical.items() if ref is None]
        if missing:
            raise RuntimeError(
                f"Engine UI missing widgets: {', '.join(missing)}. "
                f"Make sure engine_form.ui names match the code."
            )

        self._init_common_widget()

        self.progressBar.setVisible(False)
        self.workersSpin.setMinimum(1)
        if self.workersSpin.value() < 1:
            self.workersSpin.setValue(1)

        self._aoi_bbox = None
        # Blue colors for Engine AOI
        self._aoi_fill_color = QColor(0, 102, 255, 60)      # light blue with transparency
        self._aoi_stroke_color = QColor(0, 102, 255, 200)   # darker blue stroke
        self._aoi = AoiManager(self.iface, layer_name="Engine AOI", fill_color=self._aoi_fill_color, stroke_color=self._aoi_stroke_color)
        self._prev_tool = None  # Track previous map tool
        self._drawing_tool = None  # Track active drawing tool

        # Convert dropdown to 3 options at runtime (no .ui change required)
        self.aoiModeCombo.clear()
        self.aoiModeCombo.addItems(["Map extent", "Draw rectangle", "Draw polygon"])

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
        if self._common is not None:
            return self._common.get_params()
        return {
            "start_date": self.fb_start.date().toString("yyyy-MM-dd"),
            "end_date": self.fb_end.date().toString("yyyy-MM-dd"),
            "cloud_cover": int(self.fb_cloud.value()),
            "band1": self.fb_band1.text().strip(),
            "band2": (self.fb_band2.text().strip() or None),
            "formula": self.fb_formula.text().strip(),
        }

    def _aoi_mode_changed(self, text: str):
        """Update the single action button text based on the selected mode."""
        t = (text or "").lower()
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
            "VirtuGhan Engine Help",
            "Engine computes formula/index outputs from Sentinel-2 imagery.\n\n"
            "Required fields: Start date, End date, Max cloud (%), Band 1, Formula, and AOI.\n"
            "Band 2 is optional.\n\n"
            "Set AOI first, then run Engine to generate rasters.",
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

        op_txt = (self.opCombo.currentText() or "").strip()
        operation = None if op_txt == "none" else op_txt

        if (not self.timeseriesCheck.isChecked()) and (operation is None):
            raise RuntimeError("Operation is required when 'Generate timeseries' is disabled.")

        workers = max(1, int(self.workersSpin.value()))
        out_base = (self.outputPathEdit.text() or "").strip() or QgsProcessingUtils.tempFolder()
        out_dir = os.path.join(out_base, f"virtughan_engine_{uuid.uuid4().hex[:8]}")

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

        def _on_done(ok, exc):
            self._stop_tailing()
            self._set_running(False)
            if not ok or exc:
                _log(self, f"Engine failed: {exc}", Qgis.Critical)
                QMessageBox.critical(self, "VirtuGhan", f"Engine failed:\n{exc}\n\nSee runtime.log for details.")
            else:
                extract_zipfiles(out_dir, logger=lambda m, lvl=Qgis.Info: _log(self, m, lvl), delete_archives=True)
                
                added = 0
                for root, _dirs, files in os.walk(out_dir):
                    for fn in files:
                        if fn.lower().endswith((".tif", ".tiff", ".vrt")):
                            path = os.path.join(root, fn)
                            lyr = QgsRasterLayer(path, os.path.splitext(fn)[0], "gdal")
                            if lyr.isValid():
                                QgsProject.instance().addMapLayer(lyr)
                                _log(self, f"Loaded raster: {path}")
                                added += 1
                            else:
                                _log(self, f"Failed to load raster: {path}", Qgis.Warning)
                if added == 0:
                    _log(self, "No .tif/.tiff/.vrt files found to load.")
                QMessageBox.information(self, "VirtuGhan", f"Engine finished.\nOutput: {out_dir}")

        self._current_task = _VirtughanTask("VirtuGhan Engine", params, log_path, on_done=_on_done)
        QgsApplication.taskManager().addTask(self._current_task)

    def _set_running(self, running: bool):
        self.progressBar.setVisible(running)
        self.progressBar.setRange(0, 0 if running else 1)
        self.runButton.setEnabled(not running)
        self.resetButton.setEnabled(not running)
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

    def _render_scene_footprints(self, scenes):
        self._clear_scene_footprints_layer()
        if not scenes:
            return
        project = QgsProject.instance()
        dst_crs = project.crs()
        layer = QgsVectorLayer(f"Polygon?crs={dst_crs.authid()}", "Engine Scene Footprints", "memory")
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
            return

        prov.addFeatures(feats)
        layer.updateExtents()
        try:
            sym = layer.renderer().symbol()
            sym.setColor(QColor(0, 102, 255, 20))
            sym.symbolLayer(0).setStrokeColor(QColor(0, 102, 255, 180))
            sym.symbolLayer(0).setStrokeWidth(0.4)
            layer.triggerRepaint()
            layer.emitStyleChanged()
        except Exception:
            pass

        QgsProject.instance().addMapLayer(layer)
        self._scene_footprints_layer = layer

    def _preview_matching_scenes(self):
        if engine_search_stac_api is None:
            QMessageBox.warning(self, "VirtuGhan", "search_stac_api is not available in virtughan.engine.")
            return
        try:
            params = self._collect_search_params()
        except Exception as e:
            QMessageBox.warning(self, "VirtuGhan", str(e))
            return

        _log(self, "Searching matching scenes...")
        try:
            scenes = engine_search_stac_api(
                params["bbox"],
                params["start_date"],
                params["end_date"],
                params["cloud_cover"],
            )
        except Exception as e:
            _log(self, f"Scene search failed: {e}", Qgis.Critical)
            QMessageBox.critical(self, "VirtuGhan", f"Scene search failed:\n{e}")
            return

        _log(self, f"Matching scenes found: {len(scenes)}")
        if self.showSceneFootprintsCheck.isChecked():
            self._render_scene_footprints(scenes)
            _log(self, "Scene footprints layer updated.")
        else:
            self._clear_scene_footprints_layer()
            _log(self, "Footprint display is disabled (checkbox unchecked).")
