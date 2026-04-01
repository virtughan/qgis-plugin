# virtughan_qgis/tiler/tiler_widget.py
import os
import sys
import socket
import threading
import importlib
import logging
import json
import urllib.request
from collections import deque

from qgis.PyQt import uic
from qgis.PyQt.QtCore import QDate, Qt, QTimer
from qgis.PyQt.QtGui import QPixmap
from qgis.PyQt.QtWidgets import (
    QWidget,
    QMessageBox,
    QDockWidget,
    QLabel,
    QHBoxLayout,
    QVBoxLayout,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
)
from qgis.core import QgsMessageLog, Qgis, QgsProject

from .tiler_logic import TilerLogic
from ..bootstrap import (
    RUNTIME_ROOT,
    RUNTIME_SITE_PACKAGES_DIR,
    RUNTIME_FALLBACK_ROOT,
    RUNTIME_FALLBACK_SITE_PACKAGES_DIR,
    activate_runtime_paths,
)

activate_runtime_paths()

CommonParamsWidget = None
try:
    from ..common.common_widget import CommonParamsWidget
    from ..common.common_logic import (
        default_band_list,
        index_presets_two_band,
        get_index_preset,
        match_index_preset,
    )
except Exception:
    CommonParamsWidget = None
    default_band_list = lambda: ["red", "green", "blue", "nir", "nir08", "swir16", "swir22", "rededge1", "rededge2", "rededge3"]
    index_presets_two_band = lambda: []
    get_index_preset = lambda _label: None
    match_index_preset = lambda _b1, _b2, _fx: None

FORM_CLASS, _ = uic.loadUiType(os.path.join(os.path.dirname(__file__), "tiler_form.ui"))

# Keep runtime log lines across Tiler panel close/reopen within the same QGIS session.
_TILER_UI_SESSION_LOGS = deque(maxlen=2000)


class _InProcessServerManager:
    """
    Run uvicorn INSIDE this QGIS process on a background thread (Windows-safe).
    The workers value is used as request concurrency inside the app, not uvicorn multiprocess workers.
    """
    def __init__(self):
        self._server = None
        self._thread = None
        self._running = False
        self._bound_host = None
        self._bound_port = None
        self._effective_workers = 1

    def is_running(self) -> bool:
        return bool(self._running and self._server and not getattr(self._server, "should_exit", False))

    def start(self, app_path: str, host: str = "127.0.0.1", port: int = 8002, workers: int = 4):
        """
        Start in-process uvicorn for the explicit app_path only.
        Supports 'module:function' or 'file.py:function'. No auto-discovery.
        """
        if self.is_running():
            return

        effective_workers = max(1, int(workers or 1))
        self._effective_workers = effective_workers

        # Pass desired request concurrency to tiler API on import/reload.
        try:
            os.environ["VIRTUGHAN_TILER_CONCURRENCY"] = str(effective_workers)
        except Exception:
            os.environ["VIRTUGHAN_TILER_CONCURRENCY"] = "4"

        from fastapi import FastAPI
        import uvicorn

        # Use exactly one runtime dependency root to avoid class identity mismatches
        # (e.g., rasterio _ParsedPath loaded from different runtime folders).
        runtime_pairs = [
            (RUNTIME_SITE_PACKAGES_DIR, RUNTIME_ROOT),
            (RUNTIME_FALLBACK_SITE_PACKAGES_DIR, RUNTIME_FALLBACK_ROOT),
        ]

        chosen_paths = []
        for site_pkgs, root in runtime_pairs:
            if not site_pkgs or not os.path.isdir(site_pkgs):
                continue
            virtughan_pkg = os.path.join(site_pkgs, "virtughan")
            if os.path.isdir(virtughan_pkg):
                chosen_paths = [site_pkgs, root]
                break

        if not chosen_paths:
            for site_pkgs, root in runtime_pairs:
                if site_pkgs and os.path.isdir(site_pkgs):
                    chosen_paths = [site_pkgs, root]
                    break

        for dep_path in chosen_paths:
            if dep_path and os.path.isdir(dep_path) and dep_path not in sys.path:
                sys.path.insert(0, dep_path)

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
                if module_name in sys.modules:
                    m = importlib.reload(sys.modules[module_name])
                else:
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

        def _can_bind(bind_host: str, bind_port: int) -> bool:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((bind_host, int(bind_port)))
                return True
            except OSError:
                return False
            finally:
                sock.close()

        last_err = None
        chosen_port = None
        for attempt in range(21):
            try_port = int(port) + attempt
            if _can_bind(host, try_port):
                chosen_port = try_port
                break

        if chosen_port is None:
            raise RuntimeError(f"Failed to find a free local port starting at {host}:{port}")

        try:
            self._server = _make_server(chosen_port)

            def _run():
                self._running = True
                try:
                    _log(f"In-process uvicorn: using {chosen} on http://{host}:{chosen_port}")
                    self._server.run()
                finally:
                    self._running = False

            self._thread = threading.Thread(target=_run, daemon=True)
            self._thread.start()
            self._bound_host = host
            self._bound_port = chosen_port
            return
        except Exception as e:
            last_err = e

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
        self._index_updating = False
        self._last_tiler_log_id = 0
        self._view_change_timer = QTimer(self)
        self._view_change_timer.setSingleShot(True)
        self._view_change_timer.setInterval(300)
        self._view_change_timer.timeout.connect(self._notify_view_generation_change)
        self._tiler_log_timer = QTimer(self)
        self._tiler_log_timer.setInterval(1200)
        self._tiler_log_timer.timeout.connect(self._poll_tiler_logs)
        self._canvas = None
        self._last_view_signature = None
        self._last_active_worker_limit = None
        self._preferred_workers = None

        self._init_defaults()
        self._init_index_controls()
        self._init_log_panel()
        self._wire_signals()
        self._apply_timeseries_visibility()
        self._apply_localserver_visibility()
        QgsProject.instance().layersRemoved.connect(self._on_layers_removed)
        try:
            self._canvas = self.iface.mapCanvas()
            self._canvas.extentsChanged.connect(self._on_canvas_view_changed)
            self._canvas.scaleChanged.connect(self._on_canvas_scale_changed)
            self.destroyed.connect(lambda *_: self._disconnect_canvas_signals())
        except Exception:
            pass

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
        try:
            _TILER_UI_SESSION_LOGS.append(str(msg))
        except Exception:
            pass
        try:
            if hasattr(self, "_tilerLogText") and self._tilerLogText is not None:
                self._tilerLogText.appendPlainText(msg)
        except Exception:
            pass

    def _init_log_panel(self):
        root_layout = self.findChild(QVBoxLayout, "verticalLayout_root")
        if root_layout is None:
            return

        log_title = QLabel("Tiler Runtime Log")
        log_title.setStyleSheet("font-weight: 600;")
        self._tilerLogTitle = log_title
        self._tilerLogText = QPlainTextEdit(self)
        self._tilerLogText.setReadOnly(True)
        self._tilerLogText.setMinimumHeight(72)
        self._tilerLogText.setMaximumHeight(120)
        self._tilerLogText.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        clear_btn = QPushButton("Clear Log")
        clear_btn.clicked.connect(self._clear_tiler_log)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(clear_btn)
        self._tilerLogButtons = btn_row

        insert_idx = max(0, root_layout.count() - 1)
        root_layout.insertWidget(insert_idx, log_title)
        root_layout.insertWidget(insert_idx + 1, self._tilerLogText)
        root_layout.insertLayout(insert_idx + 2, btn_row)

        # Restore buffered session logs so closing/reopening the panel keeps history.
        try:
            if _TILER_UI_SESSION_LOGS:
                self._tilerLogText.setPlainText("\n".join(_TILER_UI_SESSION_LOGS))
        except Exception:
            pass

        try:
            self.groupBoxLocal.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        except Exception:
            pass
        self._rebalance_vertical_layout()

    def _rebalance_vertical_layout(self):
        """Keep advanced controls readable by compacting log panel when needed."""
        try:
            advanced_open = bool(self.groupBoxLocal.isVisible())
        except Exception:
            advanced_open = False

        try:
            if advanced_open:
                self.groupBoxLocal.setMinimumHeight(160)
                self.groupBoxLocal.setMaximumHeight(260)
            else:
                self.groupBoxLocal.setMinimumHeight(0)
                self.groupBoxLocal.setMaximumHeight(200)
        except Exception:
            pass

        try:
            if hasattr(self, "_tilerLogText") and self._tilerLogText is not None:
                if advanced_open:
                    self._tilerLogText.setMinimumHeight(48)
                    self._tilerLogText.setMaximumHeight(80)
                else:
                    self._tilerLogText.setMinimumHeight(72)
                    self._tilerLogText.setMaximumHeight(120)
        except Exception:
            pass

        try:
            self.adjustSize()
        except Exception:
            pass

        try:
            needed_h = int(self.sizeHint().height())
            if advanced_open:
                self.setMinimumHeight(max(self.minimumHeight(), needed_h))
            else:
                self.setMinimumHeight(0)
        except Exception:
            pass

    def _clear_tiler_log(self):
        try:
            self._tilerLogText.clear()
            self._last_tiler_log_id = 0
            _TILER_UI_SESSION_LOGS.clear()
        except Exception:
            pass

    def _diag_url(self, path: str) -> str:
        base = (self.backendUrlLine.text().strip() or "http://127.0.0.1:8002").rstrip("/")
        return f"{base}{path}"

    def _http_json_get(self, url: str, timeout: float = 0.6):
        req = urllib.request.Request(url=url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read().decode("utf-8", errors="replace")
            return json.loads(data)

    def _on_canvas_view_changed(self):
        if not self._is_local_server_active():
            return
        # Debounce rapid pan/zoom bursts before bumping view generation.
        self._view_change_timer.start()

    def _on_canvas_scale_changed(self, _scale):
        self._on_canvas_view_changed()

    def _disconnect_canvas_signals(self):
        try:
            if self._canvas is None:
                return
            try:
                self._canvas.extentsChanged.disconnect(self._on_canvas_view_changed)
            except Exception:
                pass
            try:
                self._canvas.scaleChanged.disconnect(self._on_canvas_scale_changed)
            except Exception:
                pass
            self._canvas = None
        except Exception:
            pass

    def _is_local_server_active(self) -> bool:
        try:
            return bool(self.runLocalCheck.isChecked() and self.server.is_running())
        except RuntimeError:
            # Can happen if Qt has already deleted child widgets during teardown.
            return False
        except Exception:
            return False

    def _notify_view_generation_change(self):
        if not self._is_local_server_active():
            return
        sig = self._current_view_signature()
        if sig is None:
            return
        if self._last_view_signature == sig:
            return
        self._last_view_signature = sig
        try:
            self._http_json_get(self._diag_url("/diag/bump-generation?reason=view_changed"), timeout=0.5)
        except Exception:
            pass

    def _current_view_signature(self):
        try:
            if self._canvas is None:
                return None
            ext = self._canvas.extent()
            # Keep signature sensitive enough so real view changes expire stale requests quickly.
            return (
                round(float(ext.xMinimum()), 3),
                round(float(ext.yMinimum()), 3),
                round(float(ext.xMaximum()), 3),
                round(float(ext.yMaximum()), 3),
                round(float(self._canvas.scale()), 2),
            )
        except Exception:
            return None

    def _poll_tiler_logs(self):
        if not self._is_local_server_active():
            self._update_worker_status_ui(None)
            return
        try:
            payload = self._http_json_get(
                self._diag_url(f"/diag/logs?since_id={int(self._last_tiler_log_id)}"),
                timeout=0.7,
            )
        except Exception:
            self._update_worker_status_ui(None)
            return

        # Keep fixed worker display stable; no runtime auto-change.
        self._update_worker_status_ui(4)

        rows = payload.get("rows", []) if isinstance(payload, dict) else []
        if not rows:
            return
        for row in rows:
            ts = row.get("ts", "")
            lvl = str(row.get("level", "info")).upper()
            msg = row.get("message", "")
            tile = row.get("tile")
            url = row.get("url")
            err = row.get("error")
            detail = row.get("detail")
            status = row.get("status")
            response = row.get("response")
            reason = row.get("reason")
            suffix = f" [{tile}]" if tile else ""
            if url:
                suffix += f" {url}"
            if status is not None:
                suffix += f" (status={status})"
            if reason:
                suffix += f" reason={reason}"
            if response:
                suffix += f" response={response}"
            if err:
                suffix += f" ({err})"
            if detail:
                suffix += f" - {detail}"
            self._log(f"{ts} [{lvl}] {msg}{suffix}")
        self._last_tiler_log_id = int(payload.get("last_id", self._last_tiler_log_id))

    def _update_worker_status_ui(self, active_limit):
        """Keep worker UI fixed at 4 for stable behavior."""
        try:
            self.labelWorkers.setText("Workers")
            self.workersSpin.setSuffix("")
            self.workersSpin.setEnabled(False)
            self.workersSpin.setValue(4)
            self.workersSpin.setToolTip("Fixed worker count (4).")
            self._last_active_worker_limit = 4
        except Exception:
            pass

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

    def _recommended_default_workers(self) -> int:
        cpu_count = os.cpu_count() or 1
        return 2 if cpu_count <= 2 else 4

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
        self.workersSpin.setRange(1, 64)
        self.workersSpin.setValue(4)
        self.workersSpin.setEnabled(False)

    def _wire_signals(self):
        self.addLayerBtn.clicked.connect(self._on_add_layer)
        self.resetBtn.clicked.connect(self._on_reset)
        self.helpBtn.clicked.connect(self._on_help)
        self.advancedToggleBtn.clicked.connect(self._toggle_advanced)
        self.timeseriesCheck.toggled.connect(self._apply_timeseries_visibility)
        self.runLocalCheck.toggled.connect(self._apply_localserver_visibility)
        self.startServerBtn.clicked.connect(self._on_start_server)
        self.stopServerBtn.clicked.connect(self._on_stop_server)
        self.simpleModeRadio.toggled.connect(self._on_formula_mode_toggled)
        self.advancedModeRadio.toggled.connect(self._on_formula_mode_toggled)
        self.indexCombo.currentTextChanged.connect(self._on_index_changed)
        self.band1Combo.currentTextChanged.connect(self._sync_reference_from_advanced)
        self.band2Combo.currentTextChanged.connect(self._sync_reference_from_advanced)
        self.formulaLine.textChanged.connect(self._sync_reference_from_advanced)

    def _init_index_controls(self):
        self._index_presets = index_presets_two_band()
        self.indexCombo.clear()
        self.indexCombo.addItems([preset.get("label", "") for preset in self._index_presets])

        matched = match_index_preset(
            self.band1Combo.currentText().strip(),
            self.band2Combo.currentText().strip(),
            self.formulaLine.text().strip(),
        )
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

        self.labelIndex.setVisible(is_simple)
        self.indexCombo.setVisible(is_simple)
        self.labelFormulaRef.setVisible(is_simple)
        self.formulaReferenceLabel.setVisible(is_simple)

        self.labelBand1.setVisible(not is_simple)
        self.band1Combo.setVisible(not is_simple)
        self.labelBand2.setVisible(not is_simple)
        self.band2Combo.setVisible(not is_simple)
        self.labelFormula.setVisible(not is_simple)
        self.formulaLine.setVisible(not is_simple)

        # Show tip in both modes; timeseries remains Advanced-only
        tiler_tip = self.findChild(QLabel, "tilerTipLabel")
        if tiler_tip is not None:
            tiler_tip.setVisible(True)
        self.labelTimeseries.setVisible(not is_simple)
        self.timeseriesCheck.setVisible(not is_simple)

        if is_simple:
            self._apply_index_preset(self.indexCombo.currentText())
        else:
            self._sync_reference_from_advanced()

    def _sync_reference_from_advanced(self, *_):
        band1 = self.band1Combo.currentText().strip()
        band2 = self.band2Combo.currentText().strip()
        formula = self.formulaLine.text().strip()

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
            self.band1Combo.setCurrentText(preset.get("band1", ""))
            self.band2Combo.setCurrentText(preset.get("band2", ""))
            self.formulaLine.setText(preset.get("formula", ""))
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
        self._rebalance_vertical_layout()

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
            "VirtuGhan Tiles Help",
            "Tiles (Tiler) creates and adds map tiles for quick visual exploration as a basemap in QGIS.\n\n"
            "Main fields: Backend URL, Layer Name, Start Date, End Date, Cloud cover (%), Band 1, and Formula.\n"
            "Band 2 is optional.\n\n"
            "Use Time series (aggregate) + Aggregation for temporal summaries, and Advanced Options for local server controls.\n\n"
            "Workers tip: In Tiler, Workers controls request concurrency. Increasing Workers can speed up tile responses on capable machines. "
            "Start with the recommended default shown in the UI (2 on low-core devices, otherwise 4), "
            "then increase gradually if your system remains stable. If your machine slows down or becomes unstable, reduce Workers.",
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
        self.workersSpin.setValue(self._recommended_default_workers())
        self.simpleModeRadio.setChecked(True)
        self._init_index_controls()
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
            workers = 4
            self._preferred_workers = workers

            if not self.server.is_running():
                self.server.start(app_path=app_path, host=host, port=requested_port, workers=workers)
                effective_workers = getattr(self.server, "_effective_workers", workers)
                if int(self.workersSpin.value()) != int(effective_workers):
                    self.workersSpin.setValue(int(effective_workers))
                bound_port = getattr(self.server, "_bound_port", requested_port)
                if bound_port != requested_port:
                    self.portSpin.setValue(bound_port)
                self.backendUrlLine.setText(f"http://{host}:{bound_port}")
                self._log(
                    f"Local uvicorn (in-process) listening at http://{host}:{bound_port} "
                    f"(request concurrency={effective_workers})"
                )
                self._log(f"Debug links: http://{host}:{bound_port}/health, http://{host}:{bound_port}/diag/logs?since_id=0, http://{host}:{bound_port}/diag/last-error")
                self._log("Tile logs use compact [z/x/y] format; only first few requests include full URL samples.")
                self._last_tiler_log_id = 0
                self._tiler_log_timer.start()
                self._apply_localserver_visibility()
            else:
                self._log("Local server already running.")
        except Exception as e:
            QMessageBox.critical(self, "Start Server Error", str(e))

    def _on_stop_server(self):
        try:
            self.server.stop()
            self._tiler_log_timer.stop()
            self._update_worker_status_ui(None)
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
                self._tiler_log_timer.stop()
                self._log("Local server stopped (no more Tiler layers).")
                self._apply_localserver_visibility()
        except Exception:
            pass

    def closeEvent(self, event):
        self._disconnect_canvas_signals()
        try:
            self._view_change_timer.stop()
            self._tiler_log_timer.stop()
        except Exception:
            pass
        super().closeEvent(event)


class TilerDockWidget(QDockWidget):
    def __init__(self, iface, parent=None):
        super().__init__("VirtuGhan • Tiler", parent)
        self._content = TilerWidget(iface, self)
        self.setWidget(self._content)
