from __future__ import annotations

from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
)
from qgis.gui import QgsMapCanvas

from ..qt_compat import QtCompat, QTableWidgetCompat, QDialogButtonBoxCompat


class PolygonSelectionDialog(QDialog):
    def __init__(self, parent, specs, selected_fids=None, title="Select Batch Polygons"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(720, 600)
        self._specs = list(specs or [])
        self._spec_by_key = {self._key(s): s for s in self._specs}
        self._checked_keys = set()
        self._initial_fids = set(selected_fids or [])

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)
        root.addWidget(QLabel("Select the polygon features to process. The selected polygons are previewed on the map."))

        controls = QHBoxLayout()
        self.selectAllButton = QPushButton("Select All", self)
        self.selectInitialButton = QPushButton("Selected in Layer", self)
        self.clearButton = QPushButton("Clear", self)
        self.selectAllButton.clicked.connect(self._select_all)
        self.selectInitialButton.clicked.connect(self._select_initial)
        self.clearButton.clicked.connect(self._clear)
        controls.addWidget(self.selectAllButton)
        controls.addWidget(self.selectInitialButton)
        controls.addWidget(self.clearButton)
        controls.addStretch(1)
        root.addLayout(controls)

        splitter = QSplitter(QtCompat.Vertical, self)
        root.addWidget(splitter, 1)

        table_host = QWidget(splitter)
        table_layout = QVBoxLayout(table_host)
        table_layout.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(table_host)
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["Feature", "FID", "BBox"])
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidgetCompat.SelectRows)
        self.table.setSelectionMode(QTableWidgetCompat.MultiSelection)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        table_layout.addWidget(self.table)

        self.canvas = QgsMapCanvas(self)
        self.canvas.setCanvasColor(QtCompat.white)
        splitter.addWidget(self.canvas)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)

        self.buttons = QDialogButtonBox(QDialogButtonBoxCompat.Ok | QDialogButtonBoxCompat.Cancel, self)
        self.buttons.button(QDialogButtonBoxCompat.Ok).setText("Use Selected")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        root.addWidget(self.buttons)

        self._basemap = self._create_osm_basemap_layer()
        self._layer = self._build_layer()
        try:
            self.canvas.setCrsTransformEnabled(True)
            self.canvas.setDestinationCrs(self._layer.crs())
        except Exception:
            pass
        layers = [self._layer]
        if self._basemap and self._basemap.isValid():
            layers.append(self._basemap)
        self.canvas.setLayers(layers)

        self._populate_table()
        if self._initial_fids:
            self._select_initial()
        else:
            self._select_all()

    def closeEvent(self, event):
        try:
            if self._basemap and self._basemap.isValid():
                QgsProject.instance().removeMapLayer(self._basemap.id())
        except Exception:
            pass
        super().closeEvent(event)

    def selected_specs(self):
        return [s for s in self._specs if self._key(s) in self._checked_keys]

    def _key(self, spec):
        return str(spec.get("fid"))

    def _create_osm_basemap_layer(self):
        layer = QgsRasterLayer("type=xyz&url=https://tile.openstreetmap.org/{z}/{x}/{y}.png&zmin=0&zmax=19", "Batch Preview OSM", "wms")
        if layer.isValid():
            try:
                QgsProject.instance().addMapLayer(layer, False)
            except Exception:
                pass
            return layer
        return None

    def _build_layer(self):
        layer_crs = "EPSG:3857" if (self._basemap and self._basemap.isValid()) else QgsProject.instance().crs().authid()
        layer = QgsVectorLayer(f"Polygon?crs={layer_crs}", "Batch AOI Preview", "memory")
        prov = layer.dataProvider()
        prov.addAttributes([QgsField("label", QVariant.String), QgsField("fid", QVariant.String)])
        layer.updateFields()
        self._reload_layer(layer)
        try:
            sym = layer.renderer().symbol()
            sym.setColor(QColor(255, 193, 7, 45))
            sym.symbolLayer(0).setStrokeColor(QColor(255, 143, 0, 190))
            sym.symbolLayer(0).setStrokeWidth(0.55)
            layer.triggerRepaint()
            layer.emitStyleChanged()
        except Exception:
            pass
        return layer

    def _reload_layer(self, layer=None):
        layer = layer or self._layer
        prov = layer.dataProvider()
        ids = [f.id() for f in layer.getFeatures()]
        if ids:
            prov.deleteFeatures(ids)
        xform = None
        try:
            src = QgsProject.instance().crs()
            dst = layer.crs()
            if src.isValid() and dst.isValid() and src != dst:
                xform = QgsCoordinateTransform(src, dst, QgsProject.instance())
        except Exception:
            pass
        feats = []
        for spec in self._specs:
            if self._key(spec) not in self._checked_keys:
                continue
            geom = QgsGeometry(spec.get("geometry_project"))
            if xform is not None:
                try:
                    geom.transform(xform)
                except Exception:
                    continue
            feat = QgsFeature(layer.fields())
            feat.setGeometry(geom)
            feat.setAttributes([str(spec.get("label", "")), str(spec.get("fid", ""))])
            feats.append(feat)
        if feats:
            prov.addFeatures(feats)
        layer.updateExtents()
        layer.triggerRepaint()
        self._zoom()

    def _zoom(self):
        try:
            ext = self._layer.extent()
            if ext and not ext.isEmpty():
                ext.scale(1.15)
                self.canvas.setExtent(ext)
            self.canvas.refresh()
        except Exception:
            pass

    def _populate_table(self):
        self.table.setRowCount(0)
        self.table.blockSignals(True)
        for spec in self._specs:
            row = self.table.rowCount()
            self.table.insertRow(row)
            key = self._key(spec)
            label_item = QTableWidgetItem(str(spec.get("label", "")))
            label_item.setData(QtCompat.UserRole, key)
            fid_item = QTableWidgetItem(str(spec.get("fid", "")))
            bbox = spec.get("bbox") or []
            bbox_item = QTableWidgetItem(", ".join(f"{float(v):.5f}" for v in bbox) if len(bbox) == 4 else "")
            self.table.setItem(row, 0, label_item)
            self.table.setItem(row, 1, fid_item)
            self.table.setItem(row, 2, bbox_item)
        self.table.blockSignals(False)
        self.table.resizeColumnsToContents()

    def _select_all(self):
        self.table.blockSignals(True)
        self.table.selectAll()
        self.table.blockSignals(False)
        self._on_selection_changed()

    def _select_initial(self):
        self.table.blockSignals(True)
        self.table.clearSelection()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            key = str(item.data(QtCompat.UserRole)) if item else ""
            spec = self._spec_by_key.get(key)
            if spec and spec.get("fid") in self._initial_fids:
                self.table.selectRow(row)
        self.table.blockSignals(False)
        self._on_selection_changed()

    def _clear(self):
        self.table.blockSignals(True)
        self.table.clearSelection()
        self.table.blockSignals(False)
        self._on_selection_changed()

    def _on_selection_changed(self):
        keys = set()
        for idx in self.table.selectionModel().selectedRows():
            item = self.table.item(idx.row(), 0)
            if item:
                keys.add(str(item.data(QtCompat.UserRole)))
        self._checked_keys = keys
        self._reload_layer()
        try:
            self.buttons.button(QDialogButtonBoxCompat.Ok).setEnabled(bool(keys))
        except Exception:
            pass
