import os
from qgis.PyQt.QtCore import Qt, QSize
from qgis.PyQt.QtWidgets import (
    QDialog, QListWidget, QListWidgetItem, QStackedWidget,
    QHBoxLayout, QVBoxLayout, QWidget, QDockWidget,
    QFrame, QAbstractItemView, QApplication, QStyle,
    QLabel, QTextBrowser, QPushButton, QScrollArea, QSizePolicy
)
from qgis.PyQt.QtGui import QIcon, QColor, QPixmap, QPainter, QPen
from qgis.core import QgsApplication, Qgis, QgsMessageLog

from ..engine.engine_widget import EngineDockWidget
from ..extractor.extractor_widget import ExtractorDockWidget
from ..tiler.tiler_widget import TilerDockWidget  # adjust if needed


PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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
        self.setWindowTitle("VirtuGhan")
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
<h3>Compute (Engine)</h3>
<p><b>Purpose:</b> Create computed analysis rasters (for example NDVI) from Sentinel-2 imagery.</p>
<p><b>Use Compute when:</b> you want derived outputs, not only raw band downloads.</p>
<p><b>Data source:</b> Sentinel-2 scenes discovered through the VirtuGhan STAC backend.</p>
<h4>Main fields</h4>
<ul>
    <li><b>Start date *</b>, <b>End date *</b>: search period.</li>
    <li><b>Max cloud (%) *</b>: upper cloud threshold for matched scenes.</li>
    <li><b>Band 1 *</b>, <b>Band 2 (optional)</b>, <b>Formula *</b>: expression inputs and calculation formula.</li>
    <li><b>Area of Interest *</b>: choose AOI mode, then use <b>Use Canvas Extent</b> or <b>Draw AOI</b>; <b>Clear</b> resets AOI.</li>
</ul>
<h4>Options and output</h4>
<ul>
    <li><b>Aggregation</b>: reduce multiple scenes with mean/median/min/max/etc.</li>
    <li><b>Generate timeseries (GIF)</b>: export intermediate frames and GIF animation.</li>
    <li><b>Apply smart filter</b>: keep a cleaner subset of scenes before processing.</li>
    <li><b>Workers</b>: parallel processing count.</li>
    <li><b>Output folder</b>: destination directory (blank uses temporary location).</li>
    <li><b>Show matching scene footprints on map</b>: add matched scene footprints after run.</li>
    <li><b>Preview Matching Scenes</b>: review matching scenes before running.</li>
    <li><b>Run Engine</b>, <b>Reset</b>, and <b>Log</b>: execute, clear inputs, and inspect status/errors.</li>
</ul>
<p><i>* Required fields</i></p>
<p>To learn more about VirtuGhan, visit <a href="https://github.com/virtughan">GitHub</a> or <a href="https://virtughan.com">virtughan.com</a>.</p>
""",
            "extractor": """
<h3>Download (Extractor)</h3>
<p><b>Purpose:</b> Download raw Sentinel-2 bands to local files.</p>
<p><b>Use Download when:</b> you need source raster files to keep, share, or process externally.</p>
<p><b>Data source:</b> Sentinel-2 scenes discovered through the VirtuGhan STAC backend.</p>
<h4>Main fields</h4>
<ul>
    <li><b>Start date *</b>, <b>End date *</b>: search period.</li>
    <li><b>Max cloud (%) *</b>: upper cloud threshold for matched scenes.</li>
    <li><b>Band 1</b>, <b>Band 2</b>, <b>Formula</b>: shared Common Parameters used for consistent scene preview/filter context.</li>
    <li><b>Bands to extract *</b>: select one or more Sentinel-2 bands to download.</li>
    <li><b>Area of Interest *</b>: choose AOI mode, then use <b>Use Canvas Extent</b> or <b>Draw AOI</b>; <b>Clear</b> resets AOI.</li>
</ul>
<h4>Options and output</h4>
<ul>
    <li><b>Apply smart filter</b>: reduce overlapping/duplicate-like scene selection before download.</li>
    <li><b>Workers</b>: parallel download/processing count.</li>
    <li><b>Output folder</b>: destination directory for downloaded rasters.</li>
    <li><b>Show matching scene footprints on map</b>: add matched scene footprints after run.</li>
    <li><b>Preview Matching Scenes</b>: review matching scenes before running.</li>
    <li><b>Run Extractor</b>, <b>Reset</b>, and <b>Log</b>: execute, clear inputs, and inspect status/errors.</li>
</ul>
<p><i>Note: ZIP output option is removed in this version.</i></p>
<p><i>* Required fields</i></p>
<p>To learn more about VirtuGhan, visit <a href="https://github.com/virtughan">GitHub</a> or <a href="https://virtughan.com">virtughan.com</a>.</p>
""",
            "tiler": """
<h3>Tiles (Tiler)</h3>
<p><b>Purpose:</b> Add a fast XYZ basemap preview of Sentinel-2 results.</p>
<p><b>Use Tiles when:</b> you want quick visual exploration before Download or Compute.</p>
<p><b>Data source:</b> Sentinel-2 rendered through the VirtuGhan tile backend.</p>
<h4>Connection and parameters</h4>
<ul>
    <li><b>Backend URL *</b>: tile service endpoint.</li>
    <li><b>Layer Name *</b>: name shown in the QGIS Layers panel.</li>
    <li><b>Start Date *</b>, <b>End Date *</b>, <b>Cloud cover (%) *</b>: scene filter settings.</li>
    <li><b>Band 1 *</b>, <b>Band 2</b>, <b>Formula *</b>: expression used for tile rendering.</li>
    <li><b>Time series (aggregate)</b>: enable temporal aggregation; then choose <b>Aggregation</b>.</li>
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
        }

        root = QHBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)
        self._root_layout = root

        self.nav = QListWidget()
        self.nav.setObjectName("virtNav")
        self.nav.setSelectionMode(self.nav.SingleSelection)
        self.nav.setAlternatingRowColors(False)
        self.nav.setFixedWidth(120)
        self.nav.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.nav.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.nav.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.nav.setFocusPolicy(Qt.NoFocus)
        self.nav.setFrameShape(QFrame.NoFrame)
        self.nav.setIconSize(QSize(18, 18))
        self.nav.setSpacing(0) 

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

        self._add_page("Compute",  EngineDockWidget(self.iface),    make_tab_icon("engine"))
        self._add_page("Download", ExtractorDockWidget(self.iface), make_tab_icon("extractor"))
        self._add_page("Tiles",    TilerDockWidget(self.iface),     make_tab_icon("tiler"))

        self.nav.currentRowChanged.connect(self._on_nav_changed)

        # select initial page
        start_index = {"engine": 0, "extractor": 1, "tiler": 2}.get(start_page.lower(), 0)
        self.nav.setCurrentRow(start_index)
        self._set_help_for_index(start_index)

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
                padding: 4px 8px;                
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

    def _on_nav_changed(self, index: int):
        self.pages.setCurrentIndex(index)
        self._set_help_for_index(index)

    def _set_help_for_index(self, index: int):
        keys = ["engine", "extractor", "tiler"]
        key = keys[index] if 0 <= index < len(keys) else "engine"
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
            index = {"engine": 0, "extractor": 1, "tiler": 2}[k]
            if self.nav.currentRow() != index:
                self.nav.setCurrentRow(index)
        except Exception:
            pass

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

    def _add_page(self, title: str, dock: QDockWidget, icon: QIcon):
        # Strip dock chrome so it looks like a plain page
        dock.setFeatures(QDockWidget.NoDockWidgetFeatures)
        dock.setAllowedAreas(Qt.NoDockWidgetArea)
        dock.setTitleBarWidget(QWidget(dock)) 

        # Keep a stable content size so shrinking the dialog produces scrollbars
        # instead of squeezing form controls.
        try:
            content = dock.widget()
            if content is not None:
                content_min = content.sizeHint()
                content.setMinimumSize(content_min)
                dock.setMinimumSize(content_min)
            else:
                dock.setMinimumSize(dock.sizeHint())
        except Exception:
            pass

        scroller = QScrollArea()
        scroller.setObjectName("virtPageScroller")
        scroller.setWidgetResizable(True)
        scroller.setFrameShape(QFrame.NoFrame)
        scroller.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroller.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroller.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        scroller.setWidget(dock)

        # Wrap the dock in a plain QWidget page
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(scroller)
        self.pages.addWidget(page)

        # Sidebar item with enforced height
        item = QListWidgetItem(icon, title)
        item.setSizeHint(QSize(200, 32))  
        self.nav.addItem(item)
