import os
from qgis.PyQt.QtCore import Qt, QSize
from qgis.PyQt.QtWidgets import (
    QDialog, QListWidget, QListWidgetItem, QStackedWidget,
    QHBoxLayout, QVBoxLayout, QWidget, QDockWidget,
    QFrame, QAbstractItemView, QApplication, QStyle,
    QLabel, QTextBrowser, QPushButton, QScrollArea, QSizePolicy,
    QStyledItemDelegate
)
from qgis.PyQt.QtGui import QIcon, QColor, QPixmap, QPainter, QPen
from qgis.core import QgsApplication, Qgis, QgsMessageLog

from ..engine.engine_widget import EngineDockWidget
from ..extractor.extractor_widget import ExtractorDockWidget
from ..tiler.tiler_widget import TilerDockWidget  # adjust if needed
from .geocoding_widget import GeocodingPlaceWidget
from .results_widget import ResultsWidget


PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAV_BUSY_ROLE = Qt.UserRole + 77


class _NavBusyDelegate(QStyledItemDelegate):
    def __init__(self, parent=None):
        super().__init__(parent)
        icon_path = os.path.join(PLUGIN_ROOT, "static", "images", "icons", "processing.svg")
        if os.path.exists(icon_path):
            self._busy_icon = QIcon(icon_path)
        else:
            style = parent.style() if parent else QApplication.style()
            self._busy_icon = style.standardIcon(QStyle.SP_BrowserReload)

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        if not bool(index.data(NAV_BUSY_ROLE)):
            return

        size = 12
        margin_right = 12
        x = option.rect.right() - size - margin_right
        y = option.rect.y() + (option.rect.height() - size) // 2
        self._busy_icon.paint(painter, x, y, size, size)

def load_icon(rel_path: str, fallback: QStyle.StandardPixmap = QStyle.SP_FileDialogListView) -> QIcon:

    if rel_path.startswith(":/"):
        ic = QIcon(rel_path)
        if not ic.isNull():
            return ic

    abs_path = os.path.normpath(os.path.join(PLUGIN_ROOT, rel_path))
    if os.path.exists(abs_path):
        ic = QIcon(abs_path)
        if not ic.isNull():
            return ic

    # fallback
    QgsMessageLog.logMessage(f"[VirtuGhan] Icon not found, using fallback: {abs_path}", "VirtuGhan", Qgis.Warning)
    return QApplication.style().standardIcon(fallback)


def make_tab_icon(kind: str, size: int = 18, color: QColor | None = None) -> QIcon:
    c = color or QColor(255, 255, 255)
    px = QPixmap(size, size)
    px.fill(Qt.transparent)

    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing, False)

    pen = QPen(c)
    pen.setWidth(1)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)

    k = (kind or "").lower().strip()
    if k == "engine":
        # Gear/settings icon
        p.drawEllipse(5, 5, 8, 8)
        p.drawEllipse(7, 7, 4, 4)
        p.drawLine(9, 2, 9, 4)
        p.drawLine(9, 14, 9, 16)
        p.drawLine(2, 9, 4, 9)
        p.drawLine(14, 9, 16, 9)
        p.drawLine(5, 5, 6, 6)
        p.drawLine(12, 12, 13, 13)
        p.drawLine(12, 6, 13, 5)
        p.drawLine(5, 13, 6, 12)
    elif k == "extractor":
        p.drawLine(9, 4, 9, 10)
        p.drawLine(6, 8, 9, 11)
        p.drawLine(12, 8, 9, 11)
        p.drawLine(4, 13, 14, 13)
        p.drawLine(4, 13, 4, 14)
        p.drawLine(14, 13, 14, 14)
    elif k == "places":
        p.drawEllipse(5, 3, 8, 8)
        p.drawEllipse(8, 6, 2, 2)
        p.drawLine(9, 11, 9, 15)
        p.drawLine(9, 15, 7, 12)
        p.drawLine(9, 15, 11, 12)
    else:
        p.drawRoundedRect(3, 3, 5, 5, 1.0, 1.0)
        p.drawRoundedRect(10, 3, 5, 5, 1.0, 1.0)
        p.drawRoundedRect(3, 10, 5, 5, 1.0, 1.0)
        p.drawRoundedRect(10, 10, 5, 5, 1.0, 1.0)

    p.end()
    return QIcon(px)


class VirtughanHubDialog(QDialog):
    def __init__(self, iface, start_page: str = "engine", parent=None):
        super().__init__(parent)
        self.iface = iface
        self.setWindowTitle("VirtuGhan - Satellite Data Tools")
        self._centered_once = False

        self._help_expanded_width = 300
        self._help_minimized_width = 160
        self._base_width = 680
        self._help_padding = 12
        self._height = 540
        self._compact_min_width = 560
        self._compact_min_height = 440
        self.resize(self._base_width, self._height)

        self._help_by_key = {
            "engine": """
<h3>Compute</h3>
<p><b>Purpose:</b> Create layers after computing on the satellite images eg. create and download NDVI layer in a map.</p>
<p><b>Use Compute when:</b> you want derived outputs, not only raw band/image downloads.</p>
<p><b>Data source:</b> Registry of Open Data on AWS.</p>
<h4>Main fields</h4>
<ul>
    <li><b>Start date *</b>, <b>End date *</b>: search period.</li>
    <li><b>Max cloud (%) *</b>: Search Images with cloud cover less than this value.</li>
    <li><b>Band 1 *</b>, <b>Band 2 (optional)</b>, <b>Formula *</b>: expression inputs and calculation formula. Eg. NDVI = (Red - NIR)/(Red + NIR)</li>
    <li><b>Area of Interest *</b>: Bounding box of the area where you want to work on computation. Choose AOI mode, then use the <b>Use Canvas Extent</b> or <b>Draw AOI</b>; <b>Clear</b> resets AOI.</li>
</ul>
<h4>Options and output</h4>
<ul>
    <li><b>Aggregation</b>: aggregate multiple scenes with mean/median/min/max/etc.</li>
    <li><b>Generate timeseries (GIF)</b>: export intermediate frames and GIF animation. The GIF file can be found in the project folder after the computation is complete.</li>
    <li><b>Apply smart filter</b>: if enabled chooses weekly image for frequency upto 2 months, monthly for up to 1 year , quarterly up to 3 years and semi-annually for more than 3 years. For each period it selects least cloud cover image.</li>
    <li><b>Workers</b>: parallel processing count.</li>
    <li><b>Output folder</b>: destination directory (blank uses temporary location).</li>
    <li><b>Show matching scene footprints on map</b>: add matched scene footprints after run.</li>
    <li><b>Preview Matching Scenes</b>: review matching scenes before running.</li>
    <li><b>Run Compute</b>, <b>Reset</b>, and <b>Log</b>: execute, clear inputs, and inspect status/errors.</li>
</ul>
<p><i>* Required fields</i></p>
<p>To learn more about VirtuGhan, visit <a href="https://github.com/virtughan">GitHub</a> or <a href="https://virtughan.com">virtughan.com</a>.</p>
""",
            "extractor": """
<h3>Download (Extractor)</h3>
<p><b>Purpose:</b> Download satellite image bands as raster layers to your local machine.</p>
<p><b>Use Download when:</b> you want original band/image downloads instead of computed outputs.</p>
<p><b>Data source:</b> Registry of Open Data on AWS.</p>
<h4>Main fields</h4>
<ul>
    <li><b>Start date *</b>, <b>End date *</b>: search period.</li>
    <li><b>Max cloud (%) *</b>: Search Images with cloud cover less than this value.</li>
    <li><b>Band 1</b>, <b>Band 2</b>, <b>Formula</b>: shared Common Parameters used for consistent scene preview/filter context.</li>
    <li><b>Bands to download *</b>: select one or more bands that you want to download as images.</li>
    <li><b>Area of Interest *</b>: Bounding box of the area where you want to download images. Choose AOI mode, then use the <b>Use Canvas Extent</b> or <b>Draw AOI</b>; <b>Clear</b> resets AOI.</li>
</ul>
<h4>Options and output</h4>
<ul>
    <li><b>Apply smart filter</b>: if enabled chooses weekly image for frequency upto 2 months, monthly for up to 1 year , quarterly up to 3 years and semi-annually for more than 3 years. For each period it selects least cloud cover image.</li>
    <li><b>Workers</b>: parallel download/processing count.</li>
    <li><b>Output folder</b>: destination directory for downloaded rasters.</li>
    <li><b>Show matching scene footprints on map</b>: add matched scene footprints after run.</li>
    <li><b>Preview Matching Scenes</b>: review matching scenes before running.</li>
    <li><b>Download Images</b>, <b>Reset</b>, and <b>Log</b>: execute, clear inputs, and inspect status/errors.</li>
</ul>
<p><i>Note: ZIP output option is removed in this version.</i></p>
<p><i>* Required fields</i></p>
<p>To learn more about VirtuGhan, visit <a href="https://github.com/virtughan">GitHub</a> or <a href="https://virtughan.com">virtughan.com</a>.</p>
""",
            "tiler": """
<h3>Tiles (Tiler)</h3>
<p><b>Purpose:</b> Create and add map tiles for quick visual exploration of satellite imagery in QGIS.</p>
<p><b>Use Tiles when:</b> you want a fast basemap-like layer to inspect coverage and patterns before Download or Compute.</p>
<p><b>Data source:</b> Registry of Open Data on AWS rendered through the VirtuGhan tile backend.</p>
<h4>Main fields</h4>
<ul>
    <li><b>Backend URL *</b>: tile service endpoint.</li>
    <li><b>Layer Name *</b>: name shown in the QGIS Layers panel.</li>
    <li><b>Start Date *</b>, <b>End Date *</b>: search period for source imagery.</li>
    <li><b>Cloud cover (%) *</b>: Search Images with cloud cover less than this value.</li>
    <li><b>Band 1 *</b>, <b>Band 2 (optional)</b>, <b>Formula *</b>: expression inputs and calculation formula used for tile rendering.</li>
    <li><b>Time series (aggregate)</b>: if enabled creates aggregated tile output across dates; choose <b>Aggregation</b> method.</li>
</ul>
<h4>Options and output</h4>
<ul>
    <li><b>Add XYZ Layer</b>: create/update the map tile layer in QGIS.</li>
    <li><b>Reset</b>: restore default form values.</li>
</ul>
<h4>Advanced Options</h4>
<ul>
    <li><b>Advanced Options</b>: expands/collapses Local Server settings.</li>
    <li><b>Run locally</b>: use embedded local server and auto-fill Backend URL from Host/Port.</li>
    <li><b>App Path (module:function)</b>: FastAPI app entrypoint.</li>
    <li><b>Host</b>, <b>Port</b>, <b>Workers</b>: local server binding/runtime options.</li>
    <li><b>Start server</b>, <b>Stop server</b>: control embedded local backend server.</li>
</ul>
<p><i>* Required fields</i></p>
<p>To learn more about VirtuGhan, visit <a href="https://github.com/virtughan">GitHub</a> or <a href="https://virtughan.com">virtughan.com</a>.</p>
""",
        "places": """
<h3>Places (Geocoding)</h3>
<p><b>Purpose:</b> Search locations from OpenStreetMap and jump the map canvas to your area of interest.</p>
<p><b>Use Places when:</b> you want to quickly navigate before running Compute, Download, or Tiles.</p>
<h4>How to use</h4>
<ul>
    <li>Type a place name, city, address, or landmark to get suggestions automatically.</li>
    <li>Select one result from the list and click <b>Go to Selected</b>.</li>
    <li>Double-clicking a result also moves the map.</li>
    <li>Clearing the input field resets the search and results list.</li>
</ul>
<p><i>Data source: OpenStreetMap Nominatim geocoding service.</i></p>
""",
        "results": """
<h3>Results</h3>
<p><b>Purpose:</b> View Compute image outputs directly inside VirtuGhan.</p>
<p><b>Use Results when:</b> you want a quick visual review of Aggregate, Timeseries, and Trend images without opening folders manually.</p>
<h4>Tabs</h4>
<ul>
    <li><b>Aggregate</b>: shows the aggregated preview image (for example custom_band_output_aggregate_colormap).</li>
    <li><b>Timeseries</b>: shows sequence images (suffix <b>_result_text</b>) with <b>Play</b>/<b>Pause</b> controls.</li>
    <li><b>Trend</b>: shows values-over-time plot image.</li>
</ul>
<h4>Run History</h4>
<ul>
    <li>Each Compute run is added to <b>Run History</b> at the top of Results.</li>
    <li>Select any history item to reload its Aggregate/Timeseries/Trend previews.</li>
    <li><b>Open Output Folder</b>: opens that run output location in your file explorer.</li>
    <li><b>Add Geo Outputs to Map</b>: loads georeferenced outputs from that run into the current QGIS project.</li>
    <li><b>Remove Entry</b>: removes only the selected history item from the list (does not delete files).</li>
    <li><b>Clear History</b>: removes all history items from the Results list.</li>
    <li><b>Save History</b>: saves current history to disk next to the QGIS project file.</li>
</ul>
<p><i>Session behavior: history is kept while QGIS is running. Save the QGIS project first to enable history save-to-disk.</i></p>
<p><i>Results content is refreshed after each new Compute run.</i></p>
""",
        }

        self._row_to_page = {}
        self._key_to_row = {}

        root = QHBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)
        self._root_layout = root

        self.nav = QListWidget()
        self.nav.setObjectName("virtNav")
        self.nav.setSelectionMode(self.nav.SingleSelection)
        self.nav.setAlternatingRowColors(False)
        self.nav.setFixedWidth(150)
        self.nav.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.nav.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.nav.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.nav.setFocusPolicy(Qt.NoFocus)
        self.nav.setFrameShape(QFrame.NoFrame)
        self.nav.setIconSize(QSize(24, 24))
        self.nav.setSpacing(0)
        self.nav.setItemDelegate(_NavBusyDelegate(self.nav))

        self.pages = QStackedWidget()
        self.pages.setObjectName("virtPages")

        self.helpPane = QFrame()
        self.helpPane.setObjectName("virtHelpPane")
        self._help_is_minimized = False
        self.helpPane.setMinimumWidth(0)
        self.helpPane.setMaximumWidth(0)
        self.helpPane.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        help_layout = QVBoxLayout(self.helpPane)
        help_layout.setContentsMargins(6, 6, 6, 6)
        help_layout.setSpacing(4)

        self.helpTitle = QLabel("Help")
        self.helpTitle.setObjectName("virtHelpTitle")
        self.helpTitle.setText("<b>Help</b>")
        self.helpMinButton = QPushButton("–")
        self.helpMinButton.setObjectName("virtHelpMinButton")
        self.helpMinButton.setToolTip("Minimize/restore help")
        self.helpMinButton.setFixedWidth(24)
        self.helpCloseButton = QPushButton("×")
        self.helpCloseButton.setObjectName("virtHelpCloseButton")
        self.helpCloseButton.setToolTip("Close help")
        self.helpCloseButton.setFixedWidth(24)

        top_row = QHBoxLayout()
        top_row.addWidget(self.helpTitle)
        top_row.addStretch(1)
        top_row.addWidget(self.helpMinButton)
        top_row.addWidget(self.helpCloseButton)

        self.helpText = QTextBrowser()
        self.helpText.setObjectName("virtHelpText")
        self.helpText.setOpenExternalLinks(True)
        self.helpText.setReadOnly(True)
        self.helpText.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.helpText.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        help_layout.addLayout(top_row)
        help_layout.addWidget(self.helpText, 1)

        root.addWidget(self.nav)
        root.addWidget(self.pages, 1)
        root.addWidget(self.helpPane)
        root.setStretch(0, 0)
        root.setStretch(1, 4)
        root.setStretch(2, 0)

        self.helpMinButton.clicked.connect(self._toggle_help_minimize)
        self.helpCloseButton.clicked.connect(self._close_help)
        self.helpPane.setVisible(False)
        self._set_compact_mode()

        self._add_page(
            "Compute",
            EngineDockWidget(self.iface),
            load_icon("static/images/icons/compute.svg", QStyle.SP_ComputerIcon),
            key="engine",
        )
        self._add_page(
            "Download",
            ExtractorDockWidget(self.iface),
            load_icon("static/images/icons/download.svg", QStyle.SP_ArrowDown),
            key="extractor",
        )
        self._add_page(
            "Tiles",
            TilerDockWidget(self.iface),
            load_icon("static/images/icons/tiles.svg", QStyle.SP_DirIcon),
            key="tiler",
        )
        self._add_separator()
        self._add_page(
            "Search",
            GeocodingPlaceWidget(self.iface),
            load_icon("static/images/icons/search.svg", QStyle.SP_FileDialogContentsView),
            key="places",
        )
        self._results_widget = ResultsWidget(self.iface, self)
        self._add_page(
            "Results",
            self._results_widget,
            load_icon("static/images/icons/results.svg", QStyle.SP_FileDialogDetailedView),
            key="results",
        )

        self.nav.currentRowChanged.connect(self._on_nav_changed)

        # select initial page
        start_index = self._key_to_row.get(start_page.lower(), self._key_to_row.get("engine", 0))
        self.nav.setCurrentRow(start_index)
        self._set_help_for_row(start_index)

        # Styling 
        self.setStyleSheet("""
            QDialog { background: palette(window); }

            /* LEFT NAV */
            QListWidget#virtNav, QListView#virtNav, QListView#virtNav::viewport {
                background: #494d57;             
                color: #e9e9e9;
                border: none;
                outline: none;
            }
            
            QListWidget#virtNav::item {
                padding: 8px 10px;
                font-size: 15px;
                margin: 0;
                border: none;
            }
            QListWidget#virtNav::item:hover {
                background: #2a2f38;
            }
            QListWidget#virtNav::item:selected {
                background: #394150;               
                color: #ffffff;
            }

            /* RIGHT PAGES */
            QStackedWidget#virtPages {
                background: palette(window);
            }

            QFrame#virtHelpPane {
                border: 1px solid palette(mid);
                border-radius: 3px;
                background: palette(base);
            }

            QTextBrowser#virtHelpText {
                border: none;
                background: palette(base);
            }
        """)

    def _on_nav_changed(self, row: int):
        page_index = self._row_to_page.get(row)
        if page_index is None:
            return
        self.pages.setCurrentIndex(page_index)
        self._set_help_for_row(row)

    def _set_help_for_row(self, row: int):
        key = None
        for k, mapped_row in self._key_to_row.items():
            if mapped_row == row:
                key = k
                break
        if key is None:
            key = "engine"
        self._set_help_content(key)

    def _set_help_content(self, key: str):
        k = (key or "engine").strip().lower()
        title = k.capitalize()
        text = self._help_by_key.get(k, self._help_by_key["engine"])
        self.helpTitle.setText(f"<b>{title} Help</b>")
        self.helpText.setHtml(text)

    def show_help_for(self, key: str):
        k = (key or "engine").strip().lower()
        self._set_help_content(k)
        self._help_is_minimized = False
        self._apply_help_mode(minimized=False)
        self.helpMinButton.setText("–")
        self.helpPane.setVisible(True)
        self._resize_for_help_state(visible=True)

        try:
            row = self._key_to_row[k]
            if self.nav.currentRow() != row:
                self.nav.setCurrentRow(row)
        except Exception:
            pass

    def show_results_for_output(self, output_dir: str, auto_open: bool = False, run_metadata: dict | None = None):
        summary = self._results_widget.add_result(output_dir, run_metadata=run_metadata)
        if auto_open:
            row = self._key_to_row.get("results")
            if row is not None and self.nav.currentRow() != row:
                self.nav.setCurrentRow(row)
        return summary

    def set_tab_busy(self, key: str, busy: bool):
        row = self._key_to_row.get((key or "").strip().lower())
        if row is None:
            return
        item = self.nav.item(row)
        if item is None:
            return
        item.setData(NAV_BUSY_ROLE, bool(busy))
        self.nav.viewport().update(self.nav.visualItemRect(item))

    def get_results_history_snapshot(self):
        return self._results_widget.get_history_snapshot()

    def set_results_history(self, history_entries: list[dict] | None):
        self._results_widget.set_history(history_entries)

    def _toggle_help_minimize(self):
        if not self.helpPane.isVisible():
            self.helpPane.setVisible(True)
        self._help_is_minimized = not self._help_is_minimized
        if self._help_is_minimized:
            self._apply_help_mode(minimized=True)
            self.helpMinButton.setText("□")
        else:
            self._apply_help_mode(minimized=False)
            self.helpMinButton.setText("–")
        self._resize_for_help_state(visible=True)

    def _close_help(self):
        self._apply_help_hidden()
        self.helpPane.setMinimumWidth(0)
        self.helpPane.setMaximumWidth(0)
        self.helpPane.setVisible(False)
        self._help_is_minimized = False
        self.helpMinButton.setText("–")
        self._set_compact_mode()

    def _apply_help_mode(self, minimized: bool):
        if minimized:
            self.helpPane.setMinimumWidth(110)
            self.helpPane.setMaximumWidth(260)
            self._root_layout.setStretch(1, 5)
            self._root_layout.setStretch(2, 1)
        else:
            self.helpPane.setMinimumWidth(180)
            self.helpPane.setMaximumWidth(520)
            self._root_layout.setStretch(1, 4)
            self._root_layout.setStretch(2, 2)

    def _apply_help_hidden(self):
        self._root_layout.setStretch(1, 1)
        self._root_layout.setStretch(2, 0)

    def _set_compact_mode(self):
        self.setMinimumSize(self._compact_min_width, self._compact_min_height)
        self.setMaximumSize(16777215, 16777215)
        self.resize(self._base_width, self._height)
        self._apply_help_hidden()

    def _set_expandable_mode(self):
        self.setMinimumSize(self._compact_min_width, self._compact_min_height)
        self.setMaximumSize(16777215, 16777215)

    def _resize_for_help_state(self, visible: bool):
        if not visible:
            self._set_compact_mode()
            return
        self._set_expandable_mode()
        help_w = self._help_minimized_width if self._help_is_minimized else self._help_expanded_width
        self.resize(self._base_width + help_w + self._help_padding, self.height())

    def showEvent(self, event):
        super().showEvent(event)
        if self._centered_once:
            return
        self._centered_once = True
        try:
            screen = self.windowHandle().screen() if self.windowHandle() else QApplication.primaryScreen()
            if screen is not None:
                geometry = self.frameGeometry()
                geometry.moveCenter(screen.availableGeometry().center())
                self.move(geometry.topLeft())
        except Exception:
            pass

    def _add_page(self, title: str, content_widget: QWidget, icon: QIcon, key: str):
        # Strip dock chrome so it looks like a plain page
        if isinstance(content_widget, QDockWidget):
            dock = content_widget
            dock.setFeatures(QDockWidget.NoDockWidgetFeatures)
            dock.setAllowedAreas(Qt.NoDockWidgetArea)
            dock.setTitleBarWidget(QWidget(dock))
            target_widget = dock
        else:
            target_widget = content_widget

        # Keep a stable content size so shrinking the dialog produces scrollbars
        # instead of squeezing form controls.
        try:
            if isinstance(content_widget, QDockWidget):
                content = content_widget.widget()
                if content is not None:
                    content_min = content.sizeHint()
                    content.setMinimumSize(content_min)
                    content_widget.setMinimumSize(content_min)
                else:
                    content_widget.setMinimumSize(content_widget.sizeHint())
            else:
                target_widget.setMinimumSize(target_widget.sizeHint())
        except Exception:
            pass

        scroller = QScrollArea()
        scroller.setObjectName("virtPageScroller")
        scroller.setWidgetResizable(True)
        scroller.setFrameShape(QFrame.NoFrame)
        scroller.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroller.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroller.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        scroller.setWidget(target_widget)

        # Wrap the dock in a plain QWidget page
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(scroller)
        page_index = self.pages.count()
        self.pages.addWidget(page)

        # Sidebar item with enforced height
        item = QListWidgetItem(icon, title)
        item.setSizeHint(QSize(220, 42))
        item.setData(NAV_BUSY_ROLE, False)
        row_index = self.nav.count()
        self.nav.addItem(item)
        self._row_to_page[row_index] = page_index
        self._key_to_row[(key or "").strip().lower()] = row_index

    def _add_separator(self):
        top_space = QListWidgetItem("")
        top_space.setFlags(Qt.NoItemFlags)
        top_space.setSizeHint(QSize(200, 10))
        self.nav.addItem(top_space)

        line = QListWidgetItem("")
        line.setFlags(Qt.NoItemFlags)
        line.setSizeHint(QSize(200, 14))
        self.nav.addItem(line)

        line_widget = QWidget()
        line_layout = QVBoxLayout(line_widget)
        line_layout.setContentsMargins(8, 5, 8, 5)
        line_layout.setSpacing(0)
        hline = QFrame()
        hline.setFrameShape(QFrame.HLine)
        hline.setFrameShadow(QFrame.Plain)
        hline.setStyleSheet("color: #9aa0aa;")
        line_layout.addWidget(hline)
        self.nav.setItemWidget(line, line_widget)

        bottom_space = QListWidgetItem("")
        bottom_space.setFlags(Qt.NoItemFlags)
        bottom_space.setSizeHint(QSize(200, 12))
        self.nav.addItem(bottom_space)
