# virtughan_qgis/tiler/tiler_widget.py
import os
import sys
import threading
import importlib
import logging

from qgis.PyQt import uic
from qgis.PyQt.QtCore import QDate, Qt
from qgis.PyQt.QtGui import QPixmap
from qgis.PyQt.QtWidgets import QWidget, QMessageBox, QDockWidget, QLabel, QHBoxLayout
from qgis.core import QgsMessageLog, Qgis, QgsProject

from .tiler_logic import TilerLogic

CommonParamsWidget = None
try:
    from ..common.common_widget import CommonParamsWidget
    from ..common.common_logic import default_band_list
except Exception:
    CommonParamsWidget = None

FORM_CLASS, _ = uic.loadUiType(os.path.join(os.path.dirname(__file__), "tiler_form.ui"))


class _InProcessServerManager:
    """
    Run uvicorn INSIDE this QGIS process on a background thread (Windows-safe).
    Workers are forced to 1. If you need multi-workers, run uvicorn externally.
    """
    def __init__(self):
        self._server = None
        self._thread = None
        self._running = False
        self._bound_host = None
        self._bound_port = None

    def is_running(self) -> bool:
        return bool(self._running and self._server and not getattr(self._server, "should_exit", False))

    def start(self, app_path: str, host: str = "127.0.0.1", port: int = 8002, workers: int = 1):
        """
        Start in-process uvicorn for the explicit app_path only.
        Supports 'module:function' or 'file.py:function'. No auto-discovery.
        """
        if self.is_running():
            return

        from fastapi import FastAPI
        import uvicorn

        def _log(msg: str):
            QgsMessageLog.logMessage(msg, "VirtuGhan", Qgis.Info)

        def _resolve_app(path: str):
            if not path or ":" not in path:
                return None, None
            mod_raw, fn_raw = path.split(":", 1)
            mod_raw, fn = mod_raw.strip(), fn_raw.strip()

            # file.py:function
            if mod_raw.lower().endswith(".py") or ("\\" in mod_raw) or ("/" in mod_raw):
                full = os.path.abspath(os.path.expanduser(mod_raw))
                if not os.path.isfile(full):
                    _log(f"[uvicorn] File not found: {full}")
                    return None, None
                app_dir = os.path.dirname(full)
                module_name = os.path.splitext(os.path.basename(full))[0]
                if app_dir not in sys.path:
                    sys.path.insert(0, app_dir)
            else:
                module_name = mod_raw

            try:
                m = importlib.import_module(module_name)
                app = getattr(m, fn)
                if isinstance(app, FastAPI):
                    return app, f"{module_name}:{fn}"
            except Exception as e:
                _log(f"[uvicorn] Could not import {module_name}:{fn} ({e}).")
            return None, None

        app, chosen = _resolve_app(app_path)
        if app is None:
            raise RuntimeError(
                "Could not import FastAPI app. Set App Path to 'virtughan_qgis.tiler.api:app' "
                "or 'C:\\path\\to\\api.py:app'."
            )

        uv_logger = logging.getLogger("uvicorn")
        uv_logger.setLevel(logging.INFO)

        class _QgisHandler(logging.Handler):
            def emit(self, record):
                try:
                    QgsMessageLog.logMessage(f"[uvicorn] {self.format(record)}", "VirtuGhan", Qgis.Info)
                except Exception:
                    pass

        for h in list(uv_logger.handlers):
            if isinstance(h, _QgisHandler):
                uv_logger.removeHandler(h)
        uv_logger.addHandler(_QgisHandler())

        def _make_server(bind_port: int):
            cfg = uvicorn.Config(
                app=app, host=host, port=int(bind_port),
                log_level="info", log_config=None, access_log=False
            )
            return uvicorn.Server(cfg)

        last_err = None
        for attempt in range(21):
            try_port = int(port) + attempt
            try:
                self._server = _make_server(try_port)

                def _run():
                    self._running = True
                    try:
                        _log(f"In-process uvicorn: using {chosen} on http://{host}:{try_port}")
                        self._server.run()
                    finally:
                        self._running = False

                self._thread = threading.Thread(target=_run, daemon=True)
                self._thread.start()
                self._bound_host = host
                self._bound_port = try_port
                return
            except OSError as oe:
                last_err = oe
                continue
            except Exception as e:
                last_err = e
                continue

        raise RuntimeError(f"Failed to start local server on {host}:{port}. Last error: {last_err}")

    def stop(self):
        if self._server is not None:
            try:
                self._server.should_exit = True
            except Exception:
                pass
        self._server = None
        self._thread = None
        self._running = False
        self._bound_host = None
        self._bound_port = None


class TilerWidget(QWidget, FORM_CLASS):
    """Dockable widget for configuring and loading the VirtuGhan Tiler."""

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.setupUi(self)
        self._set_header_logo()
        self.iface = iface
        self.logic = TilerLogic(iface)
        self.server = _InProcessServerManager()
        self._tiler_layer_id = None  # track last added tiler layer id

        self._init_defaults()
        self._wire_signals()
        self._apply_timeseries_visibility()
        self._apply_localserver_visibility()
        QgsProject.instance().layersRemoved.connect(self._on_layers_removed)

        try:
            self.setMinimumSize(self.sizeHint())
        except Exception:
            pass

    def _set_header_logo(self):
        try:
            title_label = self.findChild(QLabel, "titleLabel")
            if title_label is None:
                return
            if self.findChild(QLabel, "virtughanHeaderLogo") is not None:
                return
            header_layout = self.findChild(QHBoxLayout, "headerLayout")
            if header_layout is None:
                return
            logo_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "images", "virtughan-logo.png")
            if not os.path.exists(logo_path):
                return

            px = QPixmap(logo_path)
            if px.isNull():
                return

            logo_label = QLabel(self)
            logo_label.setObjectName("virtughanHeaderLogo")
            logo_label.setFixedSize(24, 24)
            logo_label.setAlignment(Qt.AlignCenter)
            logo_label.setPixmap(px.scaled(24, 24, Qt.KeepAspectRatio, Qt.SmoothTransformation))

            idx = header_layout.indexOf(title_label)
            header_layout.insertWidget(max(0, idx), logo_label)
            header_layout.setSpacing(6)
        except Exception:
            pass

    def _log(self, msg: str):
        QgsMessageLog.logMessage(msg, "VirtuGhan", Qgis.Info)

    # Reuse defaults from common (if available), else fallback
    def _load_common_defaults(self):
        """
        Returns dict:
          start_date, end_date = 'yyyy-MM-dd'
          cloud_cover (int), band1, band2, formula (str)
        """
        try:
            if CommonParamsWidget is not None:
                for name in ("default_values", "get_default_params", "defaults", "get_defaults"):
                    if hasattr(CommonParamsWidget, name):
                        d = getattr(CommonParamsWidget, name)()
                        if isinstance(d, dict) and d:
                            return d
        except Exception:
            pass
        today = QDate.currentDate()
        return {
            "start_date": today.addDays(-30).toString("yyyy-MM-dd"),
            "end_date": today.toString("yyyy-MM-dd"),
            "cloud_cover": 30,
            "band1": "red",
            "band2": "nir",
            "formula": "(band2 - band1) / (band2 + band1)",
        }

    def _qdate_from_any(self, v, fallback: QDate) -> QDate:
        if isinstance(v, QDate) and v.isValid():
            return v
        if isinstance(v, str):
            qd = QDate.fromString(v, "yyyy-MM-dd")
            if qd.isValid():
                return qd
        return fallback

    def _init_defaults(self):
        d = self._load_common_defaults()

        # Dates
        today = QDate.currentDate()
        sd = self._qdate_from_any(d.get("start_date"), today.addDays(-30))
        ed = self._qdate_from_any(d.get("end_date"), today)
        self.startDateEdit.setDate(sd)
        self.endDateEdit.setDate(ed)

        # Cloud
        self.cloudSpin.setRange(0, 100)
        self.cloudSpin.setValue(int(d.get("cloud_cover", 30)))

        band_list = default_band_list()

        # Bands / formula seeded from common
        if self.band1Combo.count() == 0:
            self.band1Combo.addItems(band_list)
        if self.band2Combo.count() == 0:
            self.band2Combo.addItems(band_list)
        self.band1Combo.setCurrentText(d.get("band1", "red"))
        self.band2Combo.setCurrentText(d.get("band2", "nir"))
        if not self.formulaLine.text():
            self.formulaLine.setText(d.get("formula", "(band2 - band1) / (band2 + band1)"))

        self.timeseriesCheck.setChecked(False)
        self.operationCombo.clear()
        self.operationCombo.addItems(["median", "mean", "min", "max"])

        # Backend defaults
        if not self.backendUrlLine.text():
            self.backendUrlLine.setText("http://127.0.0.1:8002")
        if not self.layerNameLine.text():
            self.layerNameLine.setText("VirtuGhan Tiler")

        # Local server config
        self.runLocalCheck.setChecked(True)
        self.appPathLine.setText("virtughan_qgis.tiler.api:app")
        if not self.hostLine.text():
            self.hostLine.setText("127.0.0.1")
        if self.portSpin.value() == 0:
            self.portSpin.setRange(1, 65535)
            self.portSpin.setValue(8002)
        if self.workersSpin.value() == 0:
            self.workersSpin.setRange(1, 64)
            self.workersSpin.setValue(1)

    def _wire_signals(self):
        self.addLayerBtn.clicked.connect(self._on_add_layer)
        self.resetBtn.clicked.connect(self._on_reset)
        self.helpBtn.clicked.connect(self._on_help)
        self.advancedToggleBtn.clicked.connect(self._toggle_advanced)
        self.timeseriesCheck.toggled.connect(self._apply_timeseries_visibility)
        self.runLocalCheck.toggled.connect(self._apply_localserver_visibility)
        self.startServerBtn.clicked.connect(self._on_start_server)
        self.stopServerBtn.clicked.connect(self._on_stop_server)

    def _apply_timeseries_visibility(self):
        show = self.timeseriesCheck.isChecked()
        self.labelOp.setVisible(show)
        self.operationCombo.setVisible(show)

    def _toggle_advanced(self):
        """Toggle visibility of the Advanced Options (Local Server) section."""
        is_visible = self.groupBoxLocal.isVisible()
        self.groupBoxLocal.setVisible(not is_visible)
        # Update button text to indicate expanded/collapsed state
        new_text = "Advanced Options ▲" if not is_visible else "Advanced Options ▼"
        self.advancedToggleBtn.setText(new_text)

    def _apply_localserver_visibility(self):
        enabled = self.runLocalCheck.isChecked()
        try:
            running = self.server.is_running()
        except Exception:
            running = False
        self.startServerBtn.setEnabled(enabled and not running)
        self.stopServerBtn.setEnabled(enabled and running)
        if enabled:
            host = (self.hostLine.text().strip() or "127.0.0.1")
            bound_port = getattr(self.server, "_bound_port", None)
            port = bound_port if bound_port else int(self.portSpin.value())
            self.backendUrlLine.setText(f"http://{host}:{port}")

    def _on_help(self):
        host = self.window()
        if host and hasattr(host, "show_help_for"):
            host.show_help_for("tiler")
            return

        QMessageBox.information(
            self,
            "VirtuGhan Tiler Help",
            "Tiler adds a preview XYZ layer from Sentinel-2 imagery.\n\n"
            "Required fields: Backend URL, Layer Name, Start Date, End Date, Cloud cover (%), Band 1, and Formula.\n\n"
            "Use Add XYZ Layer to load preview tiles into QGIS.",
        )

    def _remove_tiler_layers(self):
        """Remove the tiler layer(s) from the project."""
        prj = QgsProject.instance()
        to_remove = []

        # 1) If we remembered the layer id, remove that first.
        if getattr(self, "_tiler_layer_id", None):
            to_remove.append(self._tiler_layer_id)
            self._tiler_layer_id = None

        # 2) Fallback: find any layer that looks like our Tiler source or name.
        want_name = (self.layerNameLine.text().strip() or "VirtuGhan Tiler")
        for lyr in prj.mapLayers().values():
            try:
                src = getattr(lyr, "source", lambda: "")() or ""
                if "/tile/{z}/{x}/{y}" in src or lyr.name() == want_name:
                    if lyr.id() not in to_remove:
                        to_remove.append(lyr.id())
            except Exception:
                pass

        if to_remove:
            prj.removeMapLayers(to_remove)

    def _on_reset(self):
        # remove any added tiler layers
        self._remove_tiler_layers()
        # re-init defaults and UI
        self._init_defaults()
        self._apply_timeseries_visibility()
        self._apply_localserver_visibility()

    def _validate(self):
        if not self.backendUrlLine.text().strip():
            raise ValueError("Backend URL cannot be empty.")
        if not self.layerNameLine.text().strip():
            raise ValueError("Layer name cannot be empty.")
        if self.startDateEdit.date() > self.endDateEdit.date():
            raise ValueError("Start date must be before or equal to End date.")
        if not self.formulaLine.text().strip():
            raise ValueError("Formula cannot be empty.")
        return True

    def _collect_params(self):
        start_date = self.startDateEdit.date().toString("yyyy-MM-dd")
        end_date = self.endDateEdit.date().toString("yyyy-MM-dd")
        cloud_cover = int(self.cloudSpin.value())
        band1 = self.band1Combo.currentText().strip()
        band2 = self.band2Combo.currentText().strip()
        formula = self.formulaLine.text().strip()
        timeseries = self.timeseriesCheck.isChecked()
        operation = self.operationCombo.currentText().strip() if timeseries else None
        return (start_date, end_date, cloud_cover, band1, band2, formula, timeseries, operation)

    def _on_start_server(self):
        try:
            app_path = self.appPathLine.text().strip()
            host = self.hostLine.text().strip() or "127.0.0.1"
            requested_port = int(self.portSpin.value() or 8002)

            if not self.server.is_running():
                self.server.start(app_path=app_path, host=host, port=requested_port, workers=1)
                bound_port = getattr(self.server, "_bound_port", requested_port)
                if bound_port != requested_port:
                    self.portSpin.setValue(bound_port)
                self.backendUrlLine.setText(f"http://{host}:{bound_port}")
                self._log(f"Local uvicorn (in-process) listening at http://{host}:{bound_port}")
                self._apply_localserver_visibility()
            else:
                self._log("Local server already running.")
        except Exception as e:
            QMessageBox.critical(self, "Start Server Error", str(e))

    def _on_stop_server(self):
        try:
            self.server.stop()
            self._log("Local server stopped.")
            self._apply_localserver_visibility()
        except Exception as e:
            QMessageBox.critical(self, "Stop Server Error", str(e))

    def _on_add_layer(self):
        try:
            self._validate()
            if self.runLocalCheck.isChecked() and not self.server.is_running():
                self._on_start_server()
                if not self.server.is_running():
                    raise RuntimeError("Local server did not start. Check App Path / port.")
            backend_url = self.backendUrlLine.text().strip()
            layer_name = self.layerNameLine.text().strip()
            (start_date, end_date, cloud_cover, band1, band2, formula, timeseries, operation) = self._collect_params()
            params = self.logic.default_params(
                start_date=start_date,
                end_date=end_date,
                cloud_cover=cloud_cover,
                band1=band1,
                band2=band2,
                formula=formula,
                timeseries=timeseries,
                operation=operation,
            )
            layer = self.logic.add_xyz_layer(backend_url, layer_name, params)
            self._tiler_layer_id = layer.id()   # remember the exact layer we just added
            self._log(f"Added layer '{layer_name}' with source: {layer.source()}")
            QMessageBox.information(self, "Layer Added", f"'{layer_name}' added successfully.")
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def _on_layers_removed(self, layer_ids):
        try:
            if not self.runLocalCheck.isChecked():
                return
            still_has_tiler = any(
                "/tile/{z}/{x}/{y}" in getattr(lyr, "source", lambda: "")()
                for lyr in QgsProject.instance().mapLayers().values()
            )
            if not still_has_tiler and self.server.is_running():
                self.server.stop()
                self._log("Local server stopped (no more Tiler layers).")
                self._apply_localserver_visibility()
        except Exception:
            pass


class TilerDockWidget(QDockWidget):
    def __init__(self, iface, parent=None):
        super().__init__("VirtuGhan • Tiler", parent)
        self._content = TilerWidget(iface, self)
        self.setWidget(self._content)
