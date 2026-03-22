# virtughan_qgis/extractor/extractor_widget.py
import os
import traceback
import uuid
from datetime import datetime

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsMessageLog,
    QgsPointXY,
    QgsProcessingUtils,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsTask,
    QgsVectorLayer,
)
from qgis.gui import QgsMapCanvas
from qgis.PyQt import uic
from qgis.PyQt.QtCore import Qt, QDate, QTimer, QVariant
from qgis.PyQt.QtGui import QCursor, QColor
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDockWidget,
    QFileDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..common.aoi import (
    AoiManager,
    AoiRectTool,
    AoiPolygonTool,
    rect_to_wgs84_bbox,
    geom_to_wgs84_bbox,
)
from ..common.scene_preview_dialog import ScenePreviewDialog

COMMON_IMPORT_ERROR = None
CommonParamsWidget = None
try:
    from ..common.common_widget import CommonParamsWidget
except Exception as _e:
    COMMON_IMPORT_ERROR = _e
    CommonParamsWidget = None

EXTRACTOR_IMPORT_ERROR = None
ExtractorBackend = None
try:
    from virtughan.extract import ExtractProcessor as ExtractorBackend, search_stac_api as extractor_search_stac_api
except Exception as _e:
    EXTRACTOR_IMPORT_ERROR = _e
    ExtractorBackend = None
    extractor_search_stac_api = None

UI_PATH = os.path.join(os.path.dirname(__file__), "extractor_form.ui")
FORM_CLASS, _ = uic.loadUiType(UI_PATH)


def _log(widget, msg, level=Qgis.Info):
    QgsMessageLog.logMessage(str(msg), "VirtuGhan", level)
    try:
        widget.logText.appendPlainText(str(msg))
    except Exception:
        pass


class _ExtractorTask(QgsTask):
    def __init__(self, desc, params, log_path, on_done=None):
        super().__init__(desc, QgsTask.CanCancel)
        self.params = params
        self.log_path = log_path
        self.on_done = on_done
        self.exc = None

    def run(self):
        try:
            os.makedirs(self.params["output_dir"], exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8", buffering=1) as logf:
                logf.write(
                    f"[{datetime.now().isoformat(timespec='seconds')}] Starting Extractor\n"
                )
                logf.write(f"Params: {self.params}\n")
                extr = ExtractorBackend(
                    bbox=self.params["bbox"],
                    start_date=self.params["start_date"],
                    end_date=self.params["end_date"],
                    cloud_cover=self.params["cloud_cover"],
                    bands_list=self.params["bands_list"],
                    output_dir=self.params["output_dir"],
                    log_file=logf,
                    workers=self.params["workers"],
                    zip_output=self.params["zip_output"],
                    smart_filter=self.params["smart_filter"],
                )
                extr.extract()
                logf.write("Extractor finished.\n")
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
    """Poll a text file and append new content to a QPlainTextEdit without blocking UI."""
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
from qgis.PyQt.QtCore import Qt, QTimer


class ExtractorDockWidget(QDockWidget):
    def __init__(self, iface):
        super().__init__("VirtuGhan • Extractor", iface.mainWindow())
        self.iface = iface
        self.setObjectName("VirtuGhanExtractorDock")

        self.ui_root = QWidget(self)
        self._form_owner = FORM_CLASS()
        self._form_owner.setupUi(self.ui_root)
        self.setWidget(self.ui_root)

        f = self.ui_root.findChild
        self.progressBar = f(QProgressBar, "progressBar")
        self.runButton = f(QPushButton, "runButton")
        self.resetButton = f(QPushButton, "resetButton")
        self.helpButton = f(QPushButton, "helpButton")
        self.logText = f(QPlainTextEdit, "logText")
        self.commonHost = f(QWidget, "commonParamsContainer")

        self.aoiModeCombo = f(QComboBox, "aoiModeCombo")
        self.aoiUseCanvasButton = f(QPushButton, "aoiUseCanvasButton")
        self.aoiStartDrawButton = f(QPushButton, "aoiStartDrawButton")
        self.aoiClearButton = f(QPushButton, "aoiClearButton")
        self.aoiPreviewLabel = f(QLabel, "aoiPreviewLabel")

        self.workersSpin = f(QSpinBox, "workersSpin")
        self.outputPathEdit = f(QLineEdit, "outputPathEdit")
        self.outputBrowseButton = f(QPushButton, "outputBrowseButton")
        self.previewScenesButton = f(QPushButton, "previewScenesButton")
        self.showSceneFootprintsCheck = f(QCheckBox, "showSceneFootprintsCheck")

        self.bandsListWidget = f(QListWidget, "bandsListWidget")
        self.zipOutputCheck = f(QCheckBox, "zipOutputCheck")
        self.smartFilterCheck = f(QCheckBox, "smartFilterCheck")

        # AOI state
        self._aoi_bbox = None               # [lonmin, latmin, lonmax, latmax] (WGS84)
        self._aoi_polygon_wgs84 = None      # optional [[lon,lat], ...]
        # Green colors for Extractor AOI
        self._aoi_fill_color = QColor(76, 175, 80, 60)      # light green with transparency
        self._aoi_stroke_color = QColor(56, 142, 60, 200)   # darker green stroke
        self._aoi = AoiManager(self.iface, layer_name="Extractor AOI", fill_color=self._aoi_fill_color, stroke_color=self._aoi_stroke_color)
        self._prev_tool = None

        self._init_common_widget()
        self.progressBar.setVisible(False)
        if self.workersSpin.value() < 1:
            self.workersSpin.setValue(1)

        # AOI: unifying to 3 options, single action button
        self.aoiModeCombo.clear()
        self.aoiModeCombo.addItems(["Map extent", "Draw rectangle", "Draw polygon"])
        self.aoiUseCanvasButton.setVisible(False)  # hide legacy button

        self.aoiStartDrawButton.clicked.connect(self._aoi_action_clicked)
        self.aoiClearButton.clicked.connect(self._clear_aoi)
        self.aoiModeCombo.currentTextChanged.connect(self._aoi_mode_changed)

        self.outputBrowseButton.clicked.connect(self._browse_output)
        self.resetButton.clicked.connect(self._reset_form)
        self.runButton.clicked.connect(self._run_clicked)
        self.helpButton.clicked.connect(self._open_help)
        self.previewScenesButton.clicked.connect(self._preview_matching_scenes)
        self.showSceneFootprintsCheck.toggled.connect(self._on_show_scene_footprints_toggled)

        self._update_aoi_preview()
        self._aoi_mode_changed(self.aoiModeCombo.currentText())

        try:
            self.ui_root.setMinimumSize(self.ui_root.sizeHint())
            self.setMinimumSize(self.ui_root.sizeHint())
        except Exception:
            pass

        self._current_task = None
        self._current_log_path = None
        self._tailer = None
        self._scene_footprints_layer = None

    def _init_common_widget(self):
        if CommonParamsWidget:
            self.commonWidget = CommonParamsWidget(parent=self.commonHost)
            layout = self.commonHost.layout() or QVBoxLayout(self.commonHost)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(self.commonWidget)

            # Hide fields that don't apply to Extractor
            self._hide_bands_and_formula()
        else:
            self.commonWidget = None
            _log(self, f"CommonParamsWidget failed: {COMMON_IMPORT_ERROR}", Qgis.Warning)


    def _hide_bands_and_formula(self):
        """Hide Band 1, Band 2, and Formula widgets from CommonParamsWidget."""
        w = getattr(self, "commonWidget", None)
        if not w:
            return

        # These names must match the name in .ui:
        names = ("labelBand1", "band1Combo",
                "labelBand2", "band2Combo",
                "labelFormula", "formulaEdit",
                "hintLabel")

        for name in names:
            child = w.findChild(QWidget, name)
            if child:
                child.hide()

        # Collapse hidden rows in the common grid so no blank vertical space remains.
        try:
            grid = w.findChild(QWidget, "grid")
            if grid is None:
                from qgis.PyQt.QtWidgets import QGridLayout
                layout = w.layout()
                if isinstance(layout, QGridLayout):
                    grid_layout = layout
                else:
                    grid_layout = None
            else:
                grid_layout = grid.layout()

            if grid_layout is not None:
                for row in (2, 3, 4):
                    try:
                        grid_layout.setRowMinimumHeight(row, 0)
                        grid_layout.setRowStretch(row, 0)
                    except Exception:
                        pass
                try:
                    grid_layout.setVerticalSpacing(2)
                except Exception:
                    pass
        except Exception:
            pass

        # Optional: nudge layouts to recompute sizes
        try:
            w.updateGeometry()
            w.setMinimumHeight(w.sizeHint().height())
            self.ui_root.adjustSize()
        except Exception:
            pass



    def _get_common_params(self):
        if self.commonWidget:
            return self.commonWidget.get_params()
        return {}

    def _aoi_mode_changed(self, text):
        t = (text or "").lower()
        if "extent" in t:
            self.aoiStartDrawButton.setText("Use Canvas Extent")
            self.aoiStartDrawButton.setToolTip("Capture current map canvas extent")
        elif "rectangle" in t:
            self.aoiStartDrawButton.setText("Draw Rectangle")
            self.aoiStartDrawButton.setToolTip("Press, drag, release to draw a rectangle")
        else:
            self.aoiStartDrawButton.setText("Draw Polygon")
            self.aoiStartDrawButton.setToolTip("Left-click to add vertices; right-click/Enter/double-click to finish")

    def _aoi_action_clicked(self):
        mode = (self.aoiModeCombo.currentText() or "").lower()
        if "extent" in mode:
            self._use_canvas_extent()
        elif "rectangle" in mode:
            self._start_draw_rectangle()
        else:
            self._start_draw_polygon()

    def _use_canvas_extent(self):
        canvas: QgsMapCanvas = self.iface.mapCanvas()
        if not canvas or not canvas.extent():
            QMessageBox.warning(self, "VirtuGhan", "No map canvas extent available.")
            return

        rect = canvas.extent()
        # visible AOI (map CRS)
        self._aoi.replace_geometry(QgsGeometry.fromRect(rect))
        # processing bbox (WGS84)
        self._aoi_bbox = rect_to_wgs84_bbox(rect, QgsProject.instance())
        self._aoi_polygon_wgs84 = None
        self._update_aoi_preview()

    def _start_draw_rectangle(self):
        canvas: QgsMapCanvas = self.iface.mapCanvas()
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

            self._aoi.replace_geometry(QgsGeometry.fromRect(rect))
            self._aoi_bbox = rect_to_wgs84_bbox(rect, QgsProject.instance())
            self._aoi_polygon_wgs84 = None
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
        canvas: QgsMapCanvas = self.iface.mapCanvas()
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

            self._aoi.replace_geometry(geom_map)
            self._aoi_bbox = geom_to_wgs84_bbox(geom_map, QgsProject.instance())
            self._aoi_polygon_wgs84 = self._compute_polygon_wgs84_coords(geom_map)
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

    def _compute_polygon_wgs84_coords(self, geom_map: QgsGeometry):
        """Return outer ring coords as [[lon, lat], ...] in WGS84 for the given map-CRS geometry."""
        try:
            g = QgsGeometry(geom_map)  # clone
            g.transform(QgsCoordinateTransform(
                QgsProject.instance().crs(),
                QgsCoordinateReferenceSystem("EPSG:4326"),
                QgsProject.instance()
            ))
            poly = g.asPolygon()
            if not poly:
                mp = g.asMultiPolygon()
                ring = mp[0][0] if mp else []
            else:
                ring = poly[0]
            return [[float(p.x()), float(p.y())] for p in ring]
        except Exception:
            return None

    def _clear_aoi(self):
        # Reset drawing tool if active
        canvas: QgsMapCanvas = self.iface.mapCanvas()
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
        self._aoi_polygon_wgs84 = None
        self._update_aoi_preview()
        try:
            self._aoi.clear()
        except Exception:
            pass

    def _update_aoi_preview(self, text=None):
        if text:
            self.aoiPreviewLabel.setText(text)
            return
        if self._aoi_bbox:
            x1, y1, x2, y2 = self._aoi_bbox
            self.aoiPreviewLabel.setText(
                f"AOI (EPSG:4326): ({x1:.6f}, {y1:.6f}, {x2:.6f}, {y2:.6f})"
            )
        else:
            self.aoiPreviewLabel.setText("<i>AOI: not set yet</i>")

    def _open_help(self):
        host = self.window()
        if host and hasattr(host, "show_help_for"):
            host.show_help_for("extractor")
            return

        QMessageBox.information(
            self,
            "VirtuGhan Extractor Help",
            "Extractor downloads Sentinel-2 bands for your selected AOI and date range.\n\n"
            "Required fields: Start date, End date, Max cloud (%), Bands to extract, and AOI.\n\n"
            "Run Extractor to produce raster files in the output folder.",
        )

    def _browse_output(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Output Folder")
        if folder:
            self.outputPathEdit.setText(folder)

    def _reset_form(self):
        self._aoi_bbox = None
        self._aoi_polygon_wgs84 = None
        self._update_aoi_preview("AOI: not set yet")
        try:
            self._aoi.clear()
        except Exception:
            pass
        if self.commonWidget and hasattr(self.commonWidget, "reset"):
            try:
                self.commonWidget.reset()
            except Exception:
                pass

    def _collect_params(self):
        if ExtractorBackend is None:
            raise RuntimeError(
                f"Extractor backend import failed: {EXTRACTOR_IMPORT_ERROR}"
            )
        if not self._aoi_bbox:
            raise RuntimeError("Please set AOI before running.")

        # Strict WGS84 bbox validation
        b = self._aoi_bbox
        try:
            x1, y1, x2, y2 = map(float, b)
        except Exception:
            raise RuntimeError(f"AOI bbox must be four numbers: {b}")
        if not (-180.0 <= x1 < x2 <= 180.0 and -90.0 <= y1 < y2 <= 90.0):
            raise RuntimeError(f"Invalid AOI bbox (WGS84): {b}")

        p = self._get_common_params()
        sdt = QDate.fromString(p["start_date"], "yyyy-MM-dd")
        edt = QDate.fromString(p["end_date"], "yyyy-MM-dd")
        if not sdt.isValid() or not edt.isValid():
            raise RuntimeError("Please pick valid start/end dates.")
        if sdt > edt:
            raise RuntimeError("Start date must be before end date.")

        selected_items = self.bandsListWidget.selectedItems()
        bands_list = [i.text().strip() for i in selected_items if i.text().strip()]
        if not bands_list:
            raise RuntimeError("Please select at least one band to extract.")

        zip_out = self.zipOutputCheck.isChecked()
        smart = self.smartFilterCheck.isChecked()

        workers = max(1, int(self.workersSpin.value()))
        out_base = (
            self.outputPathEdit.text() or ""
        ).strip() or QgsProcessingUtils.tempFolder()
        out_dir = os.path.join(out_base, f"virtughan_extractor_{uuid.uuid4().hex[:8]}")

        params = dict(
            bbox=self._aoi_bbox,
            start_date=p["start_date"],
            end_date=p["end_date"],
            cloud_cover=int(p["cloud_cover"]),
            bands_list=bands_list,
            zip_output=zip_out,
            smart_filter=smart,
            workers=workers,
            output_dir=out_dir,
        )

        if self._aoi_polygon_wgs84:
            params["polygon_wgs84"] = self._aoi_polygon_wgs84

        return params

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
            QMessageBox.critical(
                self, "VirtuGhan", f"Cannot create output folder:\n{out_dir}\n\n{e}"
            )
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
                _log(self, f"Extractor failed: {exc}", Qgis.Critical)
                QMessageBox.critical(
                    self,
                    "VirtuGhan",
                    f"Extractor failed:\n{exc}\n\nSee runtime.log for details.",
                )
            else:
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
                    _log(self, "No raster files found to load.")
                QMessageBox.information(
                    self, "VirtuGhan", f"Extractor finished.\nOutput: {out_dir}"
                )

        self._current_task = _ExtractorTask(
            "VirtuGhan Extractor", params, log_path, on_done=_on_done
        )
        QgsApplication.taskManager().addTask(self._current_task)

    def _start_tailing(self, log_path: str):
        self._current_log_path = log_path
        self._tailer = _UiLogTailer(log_path, self.logText, interval_ms=400)
        self._tailer.start()

    def _stop_tailing(self):
        if self._tailer:
            self._tailer.stop()
            self._tailer = None
        self._current_log_path = None

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
        layer = QgsVectorLayer(f"Polygon?crs={dst_crs.authid()}", "Extractor Scene Footprints", "memory")
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
            sym.setColor(QColor(0, 150, 136, 10))
            sym.symbolLayer(0).setStrokeColor(QColor(0, 150, 136, 120))
            sym.symbolLayer(0).setStrokeWidth(0.35)
            layer.triggerRepaint()
            layer.emitStyleChanged()
        except Exception:
            pass

        QgsProject.instance().addMapLayer(layer)
        self._scene_footprints_layer = layer

    def _preview_matching_scenes(self):
        if extractor_search_stac_api is None:
            QMessageBox.warning(self, "VirtuGhan", "search_stac_api is not available in virtughan.extract.")
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
            scenes = extractor_search_stac_api(
                params["bbox"],
                params["start_date"],
                params["end_date"],
                params["cloud_cover"],
            )
            _log(self, f"Matching scenes found: {len(scenes)}")

            dlg = ScenePreviewDialog(
                parent=self,
                scenes=scenes,
                title="Extractor Scene Preview",
                fill_color=QColor(0, 150, 136, 14),
                stroke_color=QColor(0, 150, 136, 170),
            )
            dlg.exec_()

            selected_scenes = dlg.selected_scenes()
            _log(self, f"Selected scenes in preview: {len(selected_scenes)}")
            if self.showSceneFootprintsCheck.isChecked():
                self._render_scene_footprints(selected_scenes)
                _log(self, "Scene footprints layer updated from preview selection.")
            else:
                self._clear_scene_footprints_layer()
                _log(self, "Footprint display is disabled (checkbox unchecked).")
        except Exception as e:
            _log(self, f"Scene search failed: {e}", Qgis.Critical)
            QMessageBox.critical(self, "VirtuGhan", f"Scene search failed:\n{e}")
        finally:
            self.progressBar.setVisible(False)
            self.progressBar.setRange(0, 1)
            self.previewScenesButton.setText(original_btn_text)
            self.previewScenesButton.setEnabled(True)
