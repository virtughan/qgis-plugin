import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QListWidget,
    QListWidgetItem,
)
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
)


class GeocodingPlaceWidget(QWidget):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self._results = []

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        title = QLabel("<b>Place Search (OSM)</b>")
        subtitle = QLabel("Find a location and move the map to it before running Compute, Download, or Tiles.")
        subtitle.setWordWrap(True)

        row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search place, city, address, or landmark")
        row.addWidget(self.search_edit, 1)

        self.results_list = QListWidget()
        self.go_btn = QPushButton("Go to Selected")
        self.status_label = QLabel("Type to search places.")
        self.status_label.setWordWrap(True)

        root.addWidget(title)
        root.addWidget(subtitle)
        root.addLayout(row)
        root.addWidget(self.results_list, 1)
        root.addWidget(self.go_btn)
        root.addWidget(self.status_label)

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(350)
        self._search_timer.timeout.connect(self.search_places)

        self.go_btn.clicked.connect(self.go_to_selected)
        self.search_edit.returnPressed.connect(self.search_places)
        self.search_edit.textChanged.connect(self._on_search_text_changed)
        self.results_list.itemDoubleClicked.connect(self.go_to_result_item)

    def _on_search_text_changed(self, text: str):
        query = (text or "").strip()
        if not query:
            self.results_list.clear()
            self._results = []
            self.status_label.setText("Type to search places.")
            return
        self.status_label.setText("Searching...")
        self._search_timer.start()

    def search_places(self):
        query = self.search_edit.text().strip()
        if not query:
            self.status_label.setText("Type to search places.")
            return

        self.status_label.setText("Searching OSM...")
        self.results_list.clear()
        self._results = []

        try:
            params = {
                "format": "jsonv2",
                "q": query,
                "limit": 10,
                "addressdetails": 1,
            }
            url = f"https://nominatim.openstreetmap.org/search?{urlencode(params)}"
            req = Request(
                url,
                headers={"User-Agent": "VirtuGhan-QGIS-Plugin/1.0 (https://virtughan.com)"},
            )
            with urlopen(req, timeout=15) as response:
                payload = response.read().decode("utf-8")
            items = json.loads(payload)
        except Exception as exc:
            self.status_label.setText(f"Search failed: {exc}")
            return

        if not items:
            self.status_label.setText("No places found. Try a broader query.")
            return

        self._results = items
        for item in items:
            display_name = item.get("display_name", "Unnamed place")
            label = QListWidgetItem(display_name)
            self.results_list.addItem(label)

        self.results_list.setCurrentRow(0)
        self.status_label.setText(f"Found {len(items)} place(s). Select one and click Go to Selected.")

    def go_to_result_item(self, item):
        row = self.results_list.row(item)
        self._go_to_row(row)

    def go_to_selected(self):
        row = self.results_list.currentRow()
        self._go_to_row(row)

    def _go_to_row(self, row: int):
        if row < 0 or row >= len(self._results):
            self.status_label.setText("Select a search result first.")
            return

        try:
            result = self._results[row]
            lon = float(result.get("lon"))
            lat = float(result.get("lat"))

            canvas = self.iface.mapCanvas()
            if canvas is None:
                self.status_label.setText("Map canvas is not available.")
                return

            crs_src = QgsCoordinateReferenceSystem("EPSG:4326")
            crs_dst = canvas.mapSettings().destinationCrs()
            transformer = QgsCoordinateTransform(crs_src, crs_dst, QgsProject.instance())

            map_point = transformer.transform(QgsPointXY(lon, lat))
            canvas.setCenter(map_point)

            bbox = result.get("boundingbox")
            if isinstance(bbox, list) and len(bbox) == 4:
                south = float(bbox[0])
                north = float(bbox[1])
                west = float(bbox[2])
                east = float(bbox[3])
                src_rect = QgsRectangle(west, south, east, north)
                dst_rect = transformer.transformBoundingBox(src_rect)
                if dst_rect.isFinite() and not dst_rect.isNull():
                    canvas.setExtent(dst_rect)
                else:
                    canvas.zoomScale(25000)
            else:
                canvas.zoomScale(25000)

            canvas.refresh()
            self.status_label.setText(f"Moved to: {result.get('display_name', 'selected place')}")
        except Exception as exc:
            self.status_label.setText(f"Could not move map: {exc}")
