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
from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform

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
_PAN_SETTLE_MS = 1500
_ZOOM_SETTLE_MS = 3000


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
        self._view_change_timer.setInterval(1500)
        self._view_change_timer.timeout.connect(self._notify_view_generation_change)
        self._motion_gate_failsafe_timer = QTimer(self)
        self._motion_gate_failsafe_timer.setSingleShot(True)
        self._motion_gate_failsafe_timer.timeout.connect(self._on_motion_gate_failsafe)
        self._tiler_log_timer = QTimer(self)
        self._tiler_log_timer.setInterval(1200)
        self._tiler_log_timer.timeout.connect(self._poll_tiler_logs)
        self._canvas = None
        self._last_view_signature = None
        self._last_active_worker_limit = None
        self._preferred_workers = None
        self._pending_motion_kind = "pan"
        self._view_motion_active = False
        self._tiler_layer_temporarily_hidden = False
        self._tiler_layer_prev_visibility = None
        self._poll_fail_count = 0
        self._poll_failure_logged = False
        self._view_event_seq = 0

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

    def _on_canvas_view_changed(self, motion_kind: str = "pan"):
        if not self._has_active_tiler_layer():
            return
        if not self._is_local_server_active():
            self._log(f"[DEBUG] View change ignored ({motion_kind}): local server inactive.")
            if self._has_active_tiler_layer():
                self._try_recover_local_server("view-change")
            return
        sig = self._current_view_signature()
        if sig is None:
            self._log(f"[DEBUG] View change ignored ({motion_kind}): no view signature.")
            return
        if self._last_view_signature == sig:
            self._log(f"[DEBUG] View change ignored ({motion_kind}): signature unchanged.")
            return

        self._view_event_seq += 1

        if motion_kind == "zoom":
            self._pending_motion_kind = "zoom"
        elif self._pending_motion_kind != "zoom":
            self._pending_motion_kind = "pan"

        settle_ms = _ZOOM_SETTLE_MS if self._pending_motion_kind == "zoom" else _PAN_SETTLE_MS
        settle_sec = settle_ms / 1000.0
        self._log(
            "[DEBUG] View change event "
            f"seq={self._view_event_seq} kind={motion_kind} "
            f"pending={self._pending_motion_kind} settle_ms={settle_ms}"
        )

        # During an active motion burst, only debounce settle; avoid bumping
        # generation on every tiny extent jitter (which can starve tile draws).
        self._last_view_signature = sig
        if not self._view_motion_active:
            # Gate requests client-side by hiding the XYZ layer during motion.
            self._set_tiler_layer_motion_gate(enabled=True)
            self._motion_gate_failsafe_timer.start(max(settle_ms + 2500, 4000))
            bumped = self._bump_view_generation(
                reason=f"view_changed_immediate_{self._pending_motion_kind}",
                settle=True,
                settle_delay_sec=settle_sec,
            )
            if isinstance(bumped, dict):
                self._log(
                    "[DEBUG] Immediate generation bump "
                    f"seq={self._view_event_seq} gen={bumped.get('generation')} "
                    f"reason={bumped.get('reason', f'view_changed_immediate_{self._pending_motion_kind}')}"
                )
            else:
                self._log(f"[WARN] Immediate generation bump failed seq={self._view_event_seq}")
            self._view_motion_active = True
        else:
            self._log(f"[DEBUG] Motion already active; debounce only seq={self._view_event_seq}")

        # Debounced settled bump starts only after pan/zoom quiet period.
        self._view_change_timer.setInterval(settle_ms)
        self._view_change_timer.start()
        self._log(f"[DEBUG] Debounce timer started seq={self._view_event_seq} interval_ms={settle_ms}")

    def _on_canvas_scale_changed(self, _scale):
        if not self._has_active_tiler_layer():
            return
        self._log(f"[DEBUG] scaleChanged event scale={_scale}")
        self._on_canvas_view_changed("zoom")

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

    def _has_active_tiler_layer(self) -> bool:
        """Return True when a tiler XYZ layer is present in the project."""
        try:
            if self._tiler_layer_id:
                layer = QgsProject.instance().mapLayer(self._tiler_layer_id)
                if layer is not None:
                    return True
            for lyr in QgsProject.instance().mapLayers().values():
                try:
                    src = getattr(lyr, "source", lambda: "")() or ""
                    if "/tile/{z}/{x}/{y}" in src:
                        return True
                except Exception:
                    pass
            return False
        except Exception:
            return False

    def _bump_view_generation(self, reason: str, settle: bool, settle_delay_sec: float | None = None):
        settle_q = "1" if settle else "0"
        delay_q = ""
        if settle and settle_delay_sec is not None:
            delay_q = f"&settle_delay_sec={float(settle_delay_sec):.3f}"
        try:
            return self._http_json_get(
                self._diag_url(f"/diag/bump-generation?reason={reason}&settle={settle_q}{delay_q}"),
                timeout=0.5,
            )
        except Exception:
            return None

    def _notify_view_generation_change(self):
        if not self._is_local_server_active():
            self._log("[DEBUG] Debounce timeout ignored: local server inactive.")
            return
        sig = self._current_view_signature()
        if sig is None:
            self._log("[DEBUG] Debounce timeout ignored: no view signature.")
            return
        if self._last_view_signature != sig:
            # View moved again before settle timer fired; keep debouncing without
            # additional immediate generation bumps.
            self._last_view_signature = sig
            settle_ms = _ZOOM_SETTLE_MS if self._pending_motion_kind == "zoom" else _PAN_SETTLE_MS
            self._view_change_timer.setInterval(settle_ms)
            self._view_change_timer.start()
            self._log(f"[DEBUG] Debounce timeout: view still moving, restarting timer interval_ms={settle_ms}")
            return

        # View appears stable; bump generation without extending settle delay.
        self._log(f"[DEBUG] Debounce timeout: view settled, pending_kind={self._pending_motion_kind}")
        settled = self._bump_view_generation(reason="view_settled", settle=False)
        try:
            if isinstance(settled, dict) and "generation" in settled:
                self._log(f"[DEBUG] Settled generation bump ok gen={settled.get('generation')}")
                self._publish_settled_viewport(int(settled.get("generation")))
            else:
                self._log("[WARN] Settled generation bump failed (no response).")
        except Exception:
            pass
        try:
            self._motion_gate_failsafe_timer.stop()
        except Exception:
            pass
        self._set_tiler_layer_motion_gate(enabled=False)
        self._pending_motion_kind = "pan"
        self._view_motion_active = False

    def _on_motion_gate_failsafe(self):
        """Ensure layer visibility is restored even if settle detection gets stuck."""
        try:
            if self._tiler_layer_temporarily_hidden:
                self._log("[WARN] Motion gate failsafe restore triggered.")
                self._set_tiler_layer_motion_gate(enabled=False)
                self._view_motion_active = False
                self._pending_motion_kind = "pan"
        except Exception:
            pass

    def _set_tiler_layer_motion_gate(self, enabled: bool):
        """Hide tiler layer during motion; restore original visibility when settled."""
        try:
            if not self._tiler_layer_id:
                return
            root = QgsProject.instance().layerTreeRoot()
            node = root.findLayer(self._tiler_layer_id) if root is not None else None
            if node is None:
                return

            if enabled:
                if self._tiler_layer_temporarily_hidden:
                    return
                self._tiler_layer_prev_visibility = bool(node.isVisible())
                if self._tiler_layer_prev_visibility:
                    node.setItemVisibilityChecked(False)
                    self._log("[INFO] Motion gate ON: tiler layer hidden during map movement.")
                self._tiler_layer_temporarily_hidden = True
                return

            if not self._tiler_layer_temporarily_hidden:
                return
            restore_visible = True if self._tiler_layer_prev_visibility is None else bool(self._tiler_layer_prev_visibility)
            node.setItemVisibilityChecked(restore_visible)
            if restore_visible:
                self._log("[INFO] Motion gate OFF: tiler layer restored after settle.")
            self._tiler_layer_temporarily_hidden = False
            self._tiler_layer_prev_visibility = None
            try:
                layer = QgsProject.instance().mapLayer(self._tiler_layer_id)
                if layer is not None:
                    layer.triggerRepaint()
                if self._canvas is not None:
                    self._canvas.refresh()
            except Exception:
                pass
        except Exception:
            pass

    def _current_view_signature(self):
        try:
            if self._canvas is None:
                return None
            ext = self._canvas.extent()
            # Quantize by display resolution to avoid perpetual settle from tiny jitter.
            mupp = 0.0
            try:
                mupp = float(self._canvas.mapUnitsPerPixel())
            except Exception:
                mupp = 0.0
            q = max(1e-6, mupp * 0.5)

            def _q(v: float) -> int:
                return int(round(float(v) / q))

            return (
                _q(float(ext.xMinimum())),
                _q(float(ext.yMinimum())),
                _q(float(ext.xMaximum())),
                _q(float(ext.yMaximum())),
                round(float(self._canvas.scale()), 3),
            )
        except Exception:
            return None

    def _current_view_bbox_lonlat(self):
        try:
            if self._canvas is None:
                return None
            ext = self._canvas.extent()
            src = self._canvas.mapSettings().destinationCrs()
            dst = QgsCoordinateReferenceSystem("EPSG:4326")
            tr = QgsCoordinateTransform(src, dst, QgsProject.instance())
            corners = [
                (float(ext.xMinimum()), float(ext.yMinimum())),
                (float(ext.xMinimum()), float(ext.yMaximum())),
                (float(ext.xMaximum()), float(ext.yMinimum())),
                (float(ext.xMaximum()), float(ext.yMaximum())),
            ]
            pts = [tr.transform(x, y) for x, y in corners]
            lons = [float(p.x()) for p in pts]
            lats = [float(p.y()) for p in pts]
            min_lon = min(lons)
            max_lon = max(lons)
            min_lat = min(lats)
            max_lat = max(lats)
            return (min_lon, min_lat, max_lon, max_lat)
        except Exception:
            return None

    def _publish_settled_viewport(self, generation: int):
        bbox = self._current_view_bbox_lonlat()
        if bbox is None:
            self._log(f"[WARN] Settled viewport publish skipped: bbox unavailable for gen={int(generation)}")
            return
        min_lon, min_lat, max_lon, max_lat = bbox
        self._log(
            "[INFO] Publishing settled viewport "
            f"gen={int(generation)} "
            f"bbox=({min_lon:.6f},{min_lat:.6f},{max_lon:.6f},{max_lat:.6f})"
        )
        url = self._diag_url(
            "/diag/set-viewport"
            f"?generation={int(generation)}"
            f"&min_lon={min_lon:.8f}"
            f"&min_lat={min_lat:.8f}"
            f"&max_lon={max_lon:.8f}"
            f"&max_lat={max_lat:.8f}"
            "&pad_deg=0.00"
            "&pad_tiles=1"
        )
        last_err = None
        for timeout_s in (0.9, 1.2, 1.6):
            try:
                self._http_json_get(url, timeout=timeout_s)
                self._log(f"[DEBUG] Settled viewport publish success gen={int(generation)} timeout={timeout_s}")
                return
            except Exception as e:
                last_err = e
        self._log(f"[WARN] Failed to publish settled viewport: {last_err}")

    def _poll_tiler_logs(self):
        if not self._is_local_server_active():
            self._poll_fail_count += 1
            if self._poll_fail_count >= 3 and self._has_active_tiler_layer():
                self._try_recover_local_server("log-poll")
            self._update_worker_status_ui(None)
            return
        try:
            payload = self._http_json_get(
                self._diag_url(f"/diag/logs?since_id={int(self._last_tiler_log_id)}"),
                timeout=0.7,
            )
        except Exception as e:
            self._poll_fail_count += 1
            if self._poll_fail_count >= 3 and not self._poll_failure_logged:
                self._log(f"[WARN] Tiler log polling failed repeatedly: {e}")
                self._poll_failure_logged = True
            if self._poll_fail_count >= 5 and self._has_active_tiler_layer():
                self._try_recover_local_server("log-poll-timeout")
            self._update_worker_status_ui(None)
            return

        self._poll_fail_count = 0
        self._poll_failure_logged = False

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
            python_path = row.get("python")
            pid = row.get("pid")
            cwd = row.get("cwd")
            virtughan_path = row.get("virtughan")
            rasterio_path = row.get("rasterio")
            rio_tiler_path = row.get("rio_tiler")
            parse_path_id = row.get("parse_path_id")
            parse_path_impl_id = row.get("_parse_path_id")
            changed_fields = row.get("fields")
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
            if python_path:
                suffix += f" python={python_path}"
            if pid is not None:
                suffix += f" pid={pid}"
            if cwd:
                suffix += f" cwd={cwd}"
            if virtughan_path:
                suffix += f" virtughan={virtughan_path}"
            if rasterio_path:
                suffix += f" rasterio={rasterio_path}"
            if rio_tiler_path:
                suffix += f" rio_tiler={rio_tiler_path}"
            if parse_path_id is not None:
                suffix += f" parse_path_id={parse_path_id}"
            if parse_path_impl_id is not None:
                suffix += f" _parse_path_id={parse_path_impl_id}"
            if changed_fields:
                suffix += f" changed={changed_fields}"
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
            "Tiles creates a fast layer for visual exploration and can be used as a basemap in QGIS. "
            "Choose your formula or index first, then create the tile layer. "
            "You can create multiple tile layers with different indices and blend them later using layer opacity in QGIS. "
            "Tiles uses the best available imagery in your selected date range and applies your selected formula while keeping pan and zoom responsive.\n\n"
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

        # In Simple mode, selected index preset is authoritative at run time.
        if self.simpleModeRadio.isChecked():
            preset = get_index_preset(self.indexCombo.currentText())
            if not preset:
                raise ValueError("Please select a valid index preset.")
            band1 = (preset.get("band1") or "").strip()
            band2 = (preset.get("band2") or "").strip()
            formula = (preset.get("formula") or "").strip()

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
            try:
                self._motion_gate_failsafe_timer.stop()
            except Exception:
                pass
            self._set_tiler_layer_motion_gate(enabled=False)
            self.server.stop()
            self._tiler_log_timer.stop()
            self._update_worker_status_ui(None)
            self._poll_fail_count = 0
            self._poll_failure_logged = False
            self._log("Local server stopped.")
            self._apply_localserver_visibility()
        except Exception as e:
            QMessageBox.critical(self, "Stop Server Error", str(e))

    def _try_recover_local_server(self, trigger: str):
        """Best-effort local server recovery when tiler becomes unresponsive."""
        try:
            if not self.runLocalCheck.isChecked():
                return False
        except Exception:
            return False

        try:
            if self.server.is_running():
                return True
        except Exception:
            return False

        try:
            if not self._poll_failure_logged:
                self._log(f"[WARN] Local server is not running ({trigger}); attempting restart.")
                self._poll_failure_logged = True
            self._on_start_server()
            if self.server.is_running():
                self._poll_fail_count = 0
                self._poll_failure_logged = False
                self._log("[INFO] Local server recovered.")
                return True
            return False
        except Exception as e:
            self._log(f"[WARN] Local server recovery failed: {e}")
            return False

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

            mode_label = "Simple" if self.simpleModeRadio.isChecked() else "Advanced"
            preset_label = (self.indexCombo.currentText() or "").strip() if self.simpleModeRadio.isChecked() else "(custom)"
            self._log(
                "Index selection: "
                f"mode={mode_label}, "
                f"preset={preset_label}, "
                f"formula={formula}, "
                f"band1={band1}, "
                f"band2={band2 or '-'}"
            )

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
            self._tiler_layer_temporarily_hidden = False
            self._tiler_layer_prev_visibility = None
            self._log(f"Added layer '{layer_name}' with source: {layer.source()}")
            QMessageBox.information(self, "Layer Added", f"'{layer_name}' added successfully.")
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def _on_layers_removed(self, layer_ids):
        try:
            if self._tiler_layer_id and self._tiler_layer_id in set(layer_ids or []):
                self._tiler_layer_id = None
                self._tiler_layer_temporarily_hidden = False
                self._tiler_layer_prev_visibility = None
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
            self._motion_gate_failsafe_timer.stop()
            self._tiler_log_timer.stop()
        except Exception:
            pass
        super().closeEvent(event)


class TilerDockWidget(QDockWidget):
    def __init__(self, iface, parent=None):
        super().__init__("VirtuGhan • Tiler", parent)
        self._content = TilerWidget(iface, self)
        self.setWidget(self._content)
