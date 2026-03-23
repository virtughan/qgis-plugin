import os
import json
from pathlib import Path

from qgis.PyQt.QtCore import Qt, QTimer, QUrl
from qgis.PyQt.QtGui import QPixmap, QDesktopServices
from qgis.PyQt.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QTabWidget,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
)
from qgis.core import QgsProject, QgsRasterLayer


class _ScaledImageLabel(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._image_path = None
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumHeight(220)
        self.setWordWrap(True)

    def set_image_path(self, image_path: str | None, empty_text: str = "No image found"):
        self._image_path = image_path if image_path and os.path.exists(image_path) else None
        if not self._image_path:
            self.setText(empty_text)
            self.setPixmap(QPixmap())
            return
        self._render()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._image_path:
            self._render()

    def _render(self):
        pix = QPixmap(self._image_path)
        if pix.isNull():
            self.setText(f"Could not load image:\n{self._image_path}")
            self.setPixmap(QPixmap())
            return
        shown = pix.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.setPixmap(shown)
        self.setToolTip(self._image_path)


class ResultsWidget(QWidget):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self._output_dir = None
        self._run_metadata = {}
        self._history = []
        self._timeseries_frames = []
        self._frame_index = 0

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        self.title = QLabel("<b>Engine Results</b>")
        self.btn_open_folder = QPushButton("Open Output Folder")
        self.btn_open_folder.setEnabled(False)
        self.btn_open_folder.clicked.connect(self._open_output_folder)
        self.btn_add_to_map = QPushButton("Add Geo Outputs to Map")
        self.btn_add_to_map.setEnabled(False)
        self.btn_add_to_map.clicked.connect(self._add_geo_outputs_to_map)
        self.btn_remove_entry = QPushButton("Remove Entry")
        self.btn_remove_entry.setEnabled(False)
        self.btn_remove_entry.clicked.connect(self._remove_selected_history)
        self.btn_clear_history = QPushButton("Clear History")
        self.btn_clear_history.setEnabled(False)
        self.btn_clear_history.clicked.connect(self._clear_history)
        self.btn_help = QPushButton("Help")
        self.btn_help.clicked.connect(self._open_help)
        self.btn_save_history = QPushButton("Save History")
        self.btn_save_history.clicked.connect(self._save_history)

        title_row = QHBoxLayout()
        title_row.addWidget(self.title)
        title_row.addStretch(1)
        title_row.addWidget(self.btn_save_history)
        title_row.addWidget(self.btn_clear_history)
        title_row.addWidget(self.btn_help)

        self.info = QLabel("Run Engine to see Aggregate, Timeseries, and Trend outputs here.")
        self.info.setWordWrap(True)
        self.meta = QLabel("")
        self.meta.setWordWrap(True)
        self.meta.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.history_list = QListWidget()
        self.history_list.setSelectionMode(self.history_list.SingleSelection)
        self.history_list.setMaximumHeight(120)
        self.history_list.currentRowChanged.connect(self._on_history_row_changed)

        self.entry_actions_row = QHBoxLayout()
        self.entry_actions_row.setContentsMargins(0, 0, 0, 0)
        self.entry_actions_row.setSpacing(6)
        self.entry_actions_row.addWidget(self.btn_add_to_map)
        self.entry_actions_row.addWidget(self.btn_open_folder)
        self.entry_actions_row.addWidget(self.btn_remove_entry)
        self.entry_actions_row.addStretch(1)

        self.entry_actions_host = QWidget()
        self.entry_actions_host.setLayout(self.entry_actions_row)
        self.entry_actions_host.setVisible(False)

        self.tabs = QTabWidget()
        self.tab_aggregate = QWidget()
        self.tab_timeseries = QWidget()
        self.tab_trend = QWidget()
        self.tabs.addTab(self.tab_aggregate, "Aggregate")
        self.tabs.addTab(self.tab_timeseries, "Timeseries")
        self.tabs.addTab(self.tab_trend, "Trend")

        root.addLayout(title_row)
        root.addWidget(QLabel("<b>Run History</b>"))
        root.addWidget(self.history_list)
        root.addWidget(self.entry_actions_host)
        root.addWidget(self.info)
        root.addWidget(self.meta)
        root.addWidget(self.tabs, 1)

        self.aggregate_image = _ScaledImageLabel()
        lay_agg = QVBoxLayout(self.tab_aggregate)
        lay_agg.setContentsMargins(0, 0, 0, 0)
        lay_agg.addWidget(self.aggregate_image, 1)

        self.timeseries_image = _ScaledImageLabel()
        self.btn_play = QPushButton("Play")
        self.btn_pause = QPushButton("Pause")
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setMinimum(0)
        self.slider.setMaximum(0)
        self.frame_label = QLabel("Frame: -")

        controls = QHBoxLayout()
        controls.addWidget(self.btn_play)
        controls.addWidget(self.btn_pause)
        controls.addWidget(self.slider, 1)
        controls.addWidget(self.frame_label)

        lay_ts = QVBoxLayout(self.tab_timeseries)
        lay_ts.setContentsMargins(0, 0, 0, 0)
        lay_ts.addWidget(self.timeseries_image, 1)
        lay_ts.addLayout(controls)

        self.trend_image = _ScaledImageLabel()
        lay_trend = QVBoxLayout(self.tab_trend)
        lay_trend.setContentsMargins(0, 0, 0, 0)
        lay_trend.addWidget(self.trend_image, 1)

        self._timer = QTimer(self)
        self._timer.setInterval(700)
        self._timer.timeout.connect(self._next_frame)

        self.btn_play.clicked.connect(self._start_playback)
        self.btn_pause.clicked.connect(self._pause_playback)
        self.slider.valueChanged.connect(self._on_slider_changed)

        self._set_timeseries_controls_enabled(False)

    def add_result(self, output_dir: str, run_metadata: dict | None = None):
        normalized = os.path.normpath(output_dir or "")
        if not normalized:
            return {"output_dir": output_dir, "has_any": False, "timeseries_frames": 0}

        self._prune_missing_history()
        self._history = [h for h in self._history if os.path.normpath(h.get("output_dir", "")) != normalized]
        self._history.insert(0, {
            "output_dir": normalized,
            "run_metadata": dict(run_metadata or {}),
        })
        self._refresh_history_list(select_output_dir=normalized)
        return self._show_output(normalized, run_metadata=run_metadata)

    def load_output(self, output_dir: str, run_metadata: dict | None = None):
        return self._show_output(output_dir, run_metadata=run_metadata)

    def _show_output(self, output_dir: str, run_metadata: dict | None = None):
        self._output_dir = output_dir
        self._run_metadata = dict(run_metadata or {})
        self._pause_playback()

        summary = {
            "output_dir": output_dir,
            "aggregate": None,
            "trend": None,
            "timeseries_frames": 0,
            "has_any": False,
        }

        if not output_dir or not os.path.isdir(output_dir):
            self.info.setText("Output directory not found.")
            self.meta.setText("")
            self.btn_open_folder.setEnabled(False)
            self.btn_add_to_map.setEnabled(False)
            self.aggregate_image.set_image_path(None, "No aggregate preview image found")
            self.trend_image.set_image_path(None, "No trend image found")
            self._timeseries_frames = []
            self._set_timeseries_controls_enabled(False)
            self.timeseries_image.set_image_path(None, "No timeseries images found")
            self.frame_label.setText("Frame: -")
            return summary

        root = Path(output_dir)
        self.btn_open_folder.setEnabled(True)
        self.btn_add_to_map.setEnabled(True)
        aggregate = self._find_first_image(root, starts_with=["custom_band_output_aggregate_colormap"])
        trend = self._find_first_image(root, starts_with=["values_over_time"])
        frames = self._collect_timeseries_frames(root)

        self.aggregate_image.set_image_path(aggregate, "No aggregate preview image found")
        self.trend_image.set_image_path(trend, "No trend image found")

        self._timeseries_frames = frames
        if frames:
            self.slider.blockSignals(True)
            self.slider.setMinimum(0)
            self.slider.setMaximum(len(frames) - 1)
            self.slider.setValue(0)
            self.slider.blockSignals(False)
            self._set_timeseries_controls_enabled(len(frames) > 1)
            self._frame_index = 0
            self._show_frame(0)
            if len(frames) > 1:
                self._start_playback()
        else:
            self._set_timeseries_controls_enabled(False)
            self.timeseries_image.set_image_path(None, "No timeseries images found")
            self.frame_label.setText("Frame: -")

        summary["aggregate"] = aggregate
        summary["trend"] = trend
        summary["timeseries_frames"] = len(frames)
        summary["has_any"] = bool(aggregate or trend or frames)

        msg_parts = [f"Results loaded from: {output_dir}"]
        msg_parts.append(f"Aggregate: {'yes' if aggregate else 'no'}")
        msg_parts.append(f"Timeseries frames: {len(frames)}")
        msg_parts.append(f"Trend: {'yes' if trend else 'no'}")
        self.info.setText(" | ".join(msg_parts))
        self.meta.setText(self._format_run_metadata())
        return summary

    def _refresh_history_list(self, select_output_dir: str | None = None):
        self._prune_missing_history()
        self.history_list.blockSignals(True)
        self.history_list.clear()

        selected_row = -1
        for idx, entry in enumerate(self._history):
            output_dir = entry.get("output_dir", "")
            md = entry.get("run_metadata", {}) or {}
            generated = md.get("generated_at") or "Unknown time"
            label = f"{generated} — {Path(output_dir).name}"
            item = QListWidgetItem(label)
            item.setToolTip(output_dir)
            self.history_list.addItem(item)
            if select_output_dir and os.path.normpath(output_dir) == os.path.normpath(select_output_dir):
                selected_row = idx

        self.history_list.blockSignals(False)
        has_history = len(self._history) > 0
        self.btn_clear_history.setEnabled(has_history)
        if has_history:
            if selected_row < 0:
                selected_row = 0
            self.history_list.setCurrentRow(selected_row)
        else:
            self.btn_remove_entry.setEnabled(False)

    def _prune_missing_history(self):
        self._history = [h for h in self._history if os.path.isdir(h.get("output_dir", ""))]

    def _on_history_row_changed(self, row: int):
        if row < 0 or row >= len(self._history):
            self.btn_remove_entry.setEnabled(False)
            self.btn_open_folder.setEnabled(False)
            self.btn_add_to_map.setEnabled(False)
            self.entry_actions_host.setVisible(False)
            return
        self.entry_actions_host.setVisible(True)
        self.btn_open_folder.setEnabled(True)
        self.btn_add_to_map.setEnabled(True)
        self.btn_remove_entry.setEnabled(True)
        entry = self._history[row]
        output_dir = entry.get("output_dir")
        run_metadata = entry.get("run_metadata", {})
        if not output_dir or not os.path.isdir(output_dir):
            self._prune_missing_history()
            self._refresh_history_list()
            return
        self._show_output(output_dir, run_metadata=run_metadata)

    def _remove_selected_history(self):
        row = self.history_list.currentRow()
        if row < 0 or row >= len(self._history):
            return
        removed = self._history.pop(row)
        removed_dir = removed.get("output_dir")
        self._refresh_history_list()
        if removed_dir and self._output_dir and os.path.normpath(removed_dir) == os.path.normpath(self._output_dir):
            if not self._history:
                self._show_output("", run_metadata={})

    def _clear_history(self):
        self._history = []
        self._refresh_history_list()
        self._show_output("", run_metadata={})

    def set_history(self, history_entries: list[dict] | None):
        self._history = [dict(entry) for entry in (history_entries or []) if isinstance(entry, dict)]
        self._refresh_history_list()

    def get_history_snapshot(self):
        return [dict(entry) for entry in self._history]

    def _history_save_path(self):
        project_file = QgsProject.instance().fileName() or ""
        if not project_file:
            return None
        project_path = Path(project_file)
        return str(project_path.with_suffix(".virtughan_history.json"))

    def _save_history(self):
        save_path = self._history_save_path()
        if not save_path:
            QMessageBox.information(
                self,
                "VirtuGhan",
                "Save QGIS project first if you want to save history.",
            )
            return

        self._prune_missing_history()
        payload = {
            "history": self.get_history_snapshot(),
        }
        try:
            with open(save_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            self.info.setText(f"History saved: {save_path}")
        except Exception as e:
            QMessageBox.warning(self, "VirtuGhan", f"Could not save history:\n{e}")

    def _open_help(self):
        host = self.window()
        if host and hasattr(host, "show_help_for"):
            host.show_help_for("results")
            return

        QMessageBox.information(
            self,
            "VirtuGhan Results Help",
            "Results shows Compute outputs in three tabs: Aggregate, Timeseries, and Trend.\n\n"
            "Use Run History to switch between previous runs.\n"
            "Remove Entry removes only the selected history row (files stay on disk).\n"
            "Clear History removes all rows from the list.\n"
            "Save History writes history beside the saved QGIS project file.",
        )

    def _format_run_metadata(self):
        if not self._run_metadata:
            return ""

        rows = []

        generated_at = self._run_metadata.get("generated_at")
        if generated_at:
            rows.append(f"<b>Generated:</b> {generated_at}")

        start_date = self._run_metadata.get("start_date")
        end_date = self._run_metadata.get("end_date")
        if start_date or end_date:
            rows.append(f"<b>Date range:</b> {start_date or '-'} → {end_date or '-'}")

        cloud = self._run_metadata.get("cloud_cover")
        if cloud is not None:
            rows.append(f"<b>Max cloud (%):</b> {cloud}")

        band1 = self._run_metadata.get("band1")
        band2 = self._run_metadata.get("band2")
        formula = self._run_metadata.get("formula")
        if band1 is not None:
            rows.append(f"<b>Band 1:</b> {band1}")
        if band2:
            rows.append(f"<b>Band 2:</b> {band2}")
        if formula:
            rows.append(f"<b>Formula:</b> {formula}")

        op = self._run_metadata.get("operation")
        rows.append(f"<b>Aggregation:</b> {op if op else 'none'}")

        ts = self._run_metadata.get("timeseries")
        if ts is not None:
            rows.append(f"<b>Timeseries:</b> {'enabled' if ts else 'disabled'}")

        smart = self._run_metadata.get("smart_filter")
        if smart is not None:
            rows.append(f"<b>Smart filter:</b> {'enabled' if smart else 'disabled'}")

        workers = self._run_metadata.get("workers")
        if workers is not None:
            rows.append(f"<b>Workers:</b> {workers}")

        bbox = self._run_metadata.get("bbox")
        if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
            rows.append(
                f"<b>AOI bbox (EPSG:4326):</b> "
                f"({bbox[0]:.6f}, {bbox[1]:.6f}, {bbox[2]:.6f}, {bbox[3]:.6f})"
            )

        return "<br/>".join(rows)

    def _open_output_folder(self):
        if not self._output_dir or not os.path.isdir(self._output_dir):
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(self._output_dir))

    def _add_geo_outputs_to_map(self):
        if not self._output_dir or not os.path.isdir(self._output_dir):
            self.info.setText("Output directory is not available.")
            return

        loaded = 0
        skipped = 0
        existing_sources = set()
        for lyr in QgsProject.instance().mapLayers().values():
            try:
                src = lyr.source() if hasattr(lyr, "source") else ""
                if src:
                    existing_sources.add(os.path.normcase(os.path.normpath(src)))
            except Exception:
                pass

        for root, _dirs, files in os.walk(self._output_dir):
            for fn in files:
                if not fn.lower().endswith((".tif", ".tiff", ".vrt")):
                    continue
                path = os.path.normpath(os.path.join(root, fn))
                norm_path = os.path.normcase(path)
                if norm_path in existing_sources:
                    skipped += 1
                    continue
                lyr = QgsRasterLayer(path, os.path.splitext(fn)[0], "gdal")
                if lyr.isValid():
                    QgsProject.instance().addMapLayer(lyr)
                    existing_sources.add(norm_path)
                    loaded += 1
                else:
                    skipped += 1

        self.info.setText(
            f"Geo outputs loaded to map: {loaded} | Skipped/invalid/already-present: {skipped}"
        )

    def _set_timeseries_controls_enabled(self, enabled: bool):
        self.btn_play.setEnabled(enabled)
        self.btn_pause.setEnabled(enabled)
        self.slider.setEnabled(enabled)

    def _find_first_image(self, root: Path, starts_with: list[str]):
        exts = {".png", ".jpg", ".jpeg", ".webp"}
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() not in exts:
                continue
            low = path.stem.lower()
            for prefix in starts_with:
                if low.startswith(prefix.lower()):
                    return str(path)
        return None

    def _collect_timeseries_frames(self, root: Path):
        exts = {".png", ".jpg", ".jpeg", ".webp"}
        frames = []
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in exts:
                continue
            stem = path.stem.lower()
            if "_result_text" in stem:
                frames.append(str(path))
        return sorted(frames)

    def _show_frame(self, index: int):
        if not self._timeseries_frames:
            self.timeseries_image.set_image_path(None, "No timeseries images found")
            self.frame_label.setText("Frame: -")
            return
        index = max(0, min(index, len(self._timeseries_frames) - 1))
        self._frame_index = index
        frame_path = self._timeseries_frames[index]
        self.timeseries_image.set_image_path(frame_path, "No timeseries images found")
        self.frame_label.setText(f"Frame: {index + 1}/{len(self._timeseries_frames)}")

    def _next_frame(self):
        if not self._timeseries_frames:
            self._pause_playback()
            return
        next_idx = (self._frame_index + 1) % len(self._timeseries_frames)
        self.slider.blockSignals(True)
        self.slider.setValue(next_idx)
        self.slider.blockSignals(False)
        self._show_frame(next_idx)

    def _on_slider_changed(self, value: int):
        self._show_frame(value)

    def _start_playback(self):
        if len(self._timeseries_frames) <= 1:
            return
        self._timer.start()

    def _pause_playback(self):
        self._timer.stop()
