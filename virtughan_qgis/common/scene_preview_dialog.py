from qgis.PyQt.QtCore import Qt, QVariant
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
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
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
)
from qgis.gui import QgsMapCanvas, QgsMapToolIdentifyFeature


class ScenePreviewDialog(QDialog):
    def __init__(
        self,
        parent,
        scenes,
        title,
        fill_color: QColor,
        stroke_color: QColor,
        aoi_geometry: QgsGeometry = None,
        aoi_crs_authid: str = None,
        aoi_fill_color: QColor = None,
        aoi_stroke_color: QColor = None,
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(700, 600)

        self._all_scenes = scenes or []
        self._scene_by_id = {str(s.get("id", "")): s for s in self._all_scenes}
        self._checked_ids = set(self._scene_by_id.keys())

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        info = QLabel("Top list selection controls footprint visibility in this preview map. Click a footprint to inspect attributes.")
        root.addWidget(info)

        controls_row = QHBoxLayout()
        self.selectAllButton = QPushButton("Select All", self)
        self.deselectAllButton = QPushButton("Deselect All", self)
        self.selectAllButton.clicked.connect(self._select_all_scenes)
        self.deselectAllButton.clicked.connect(self._deselect_all_scenes)
        controls_row.addWidget(self.selectAllButton)
        controls_row.addWidget(self.deselectAllButton)
        controls_row.addStretch(1)
        root.addLayout(controls_row)

        top_split = QSplitter(Qt.Horizontal, self)
        root.addWidget(top_split, 2)

        table_host = QWidget(top_split)
        table_layout = QVBoxLayout(table_host)
        table_layout.setContentsMargins(0, 0, 0, 0)

        self.table = QTableWidget(table_host)
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["Scene ID", "Datetime", "Cloud %"])
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.MultiSelection)
        self.table.itemSelectionChanged.connect(self._on_table_selection_changed)
        table_layout.addWidget(self.table)

        self.detailText = QPlainTextEdit(top_split)
        self.detailText.setReadOnly(True)
        self.detailText.setPlaceholderText("Scene attributes will appear here.")

        top_split.setStretchFactor(0, 3)
        top_split.setStretchFactor(1, 2)

        self.canvas = QgsMapCanvas(self)
        self.canvas.setCanvasColor(Qt.white)
        root.addWidget(self.canvas, 3)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Close, self)
        self.buttons.button(QDialogButtonBox.Close).setText("Close")
        self.buttons.rejected.connect(self.reject)
        root.addWidget(self.buttons)

        self._basemap = self._create_osm_basemap_layer()
        self._layer = self._build_preview_layer(fill_color, stroke_color)
        self._aoi_layer = self._build_aoi_layer(
            aoi_geometry,
            aoi_crs_authid,
            aoi_fill_color,
            aoi_stroke_color,
        )
        try:
            self.canvas.setCrsTransformEnabled(True)
            self.canvas.setDestinationCrs(self._layer.crs())
        except Exception:
            pass
        if self._basemap and self._basemap.isValid():
            layers = [self._layer]
            if self._aoi_layer and self._aoi_layer.isValid():
                layers.append(self._aoi_layer)
            layers.append(self._basemap)
            self.canvas.setLayers(layers)
        else:
            layers = [self._layer]
            if self._aoi_layer and self._aoi_layer.isValid():
                layers.append(self._aoi_layer)
            self.canvas.setLayers(layers)
        self._zoom_to_footprints()

        self._identify_tool = QgsMapToolIdentifyFeature(self.canvas, self._layer)
        self._identify_tool.featureIdentified.connect(self._on_feature_identified)
        self.canvas.setMapTool(self._identify_tool)

        self._populate_table()

    def closeEvent(self, event):
        try:
            if self._basemap and self._basemap.isValid():
                QgsProject.instance().removeMapLayer(self._basemap.id())
        except Exception:
            pass
        try:
            if self._aoi_layer and self._aoi_layer.isValid():
                QgsProject.instance().removeMapLayer(self._aoi_layer.id())
        except Exception:
            pass
        super().closeEvent(event)

    def _create_osm_basemap_layer(self):
        uri = "type=xyz&url=https://tile.openstreetmap.org/{z}/{x}/{y}.png&zmin=0&zmax=19"
        layer = QgsRasterLayer(uri, "Preview OSM Basemap", "wms")
        if not layer.isValid():
            return None
        try:
            QgsProject.instance().addMapLayer(layer, False)
        except Exception:
            pass
        return layer

    def selected_scenes(self):
        selected = []
        for scene in self._all_scenes:
            sid = str(scene.get("id", ""))
            if sid in self._checked_ids:
                selected.append(scene)
        return selected

    def _build_preview_layer(self, fill_color: QColor, stroke_color: QColor):
        layer_crs = "EPSG:3857" if (self._basemap and self._basemap.isValid()) else "EPSG:4326"
        layer = QgsVectorLayer(f"Polygon?crs={layer_crs}", "Scene Preview", "memory")
        prov = layer.dataProvider()
        prov.addAttributes([
            QgsField("scene_id", QVariant.String),
            QgsField("datetime", QVariant.String),
            QgsField("cloud", QVariant.Double),
            QgsField("collection", QVariant.String),
        ])
        layer.updateFields()

        self._reload_layer_features(layer)

        try:
            sym = layer.renderer().symbol()
            sym.setColor(fill_color)
            sym.symbolLayer(0).setStrokeColor(stroke_color)
            sym.symbolLayer(0).setStrokeWidth(0.45)
            layer.triggerRepaint()
            layer.emitStyleChanged()
        except Exception:
            pass

        return layer

    def _build_aoi_layer(self, aoi_geometry, aoi_crs_authid, fill_color: QColor, stroke_color: QColor):
        if aoi_geometry is None:
            return None

        layer_crs = "EPSG:3857" if (self._basemap and self._basemap.isValid()) else "EPSG:4326"
        layer = QgsVectorLayer(f"Polygon?crs={layer_crs}", "AOI Preview", "memory")
        if not layer.isValid():
            return None

        src_authid = aoi_crs_authid or "EPSG:4326"
        geom = QgsGeometry(aoi_geometry)
        try:
            src = QgsCoordinateReferenceSystem(src_authid)
            dst = layer.crs()
            if src.isValid() and dst.isValid() and src != dst:
                geom.transform(QgsCoordinateTransform(src, dst, QgsProject.instance()))
        except Exception:
            pass

        if geom is None or geom.isEmpty():
            return None

        prov = layer.dataProvider()
        prov.addAttributes([QgsField("label", QVariant.String)])
        layer.updateFields()
        feat = QgsFeature(layer.fields())
        feat.setGeometry(geom)
        feat.setAttributes(["AOI"])
        prov.addFeatures([feat])
        layer.updateExtents()

        fill = fill_color or QColor(0, 102, 255, 60)
        stroke = stroke_color or QColor(0, 102, 255, 200)
        try:
            sym = layer.renderer().symbol()
            sym.setColor(fill)
            sym.symbolLayer(0).setStrokeColor(stroke)
            sym.symbolLayer(0).setStrokeWidth(0.6)
            layer.triggerRepaint()
            layer.emitStyleChanged()
        except Exception:
            pass

        try:
            QgsProject.instance().addMapLayer(layer, False)
        except Exception:
            pass
        return layer

    def _reload_layer_features(self, layer):
        prov = layer.dataProvider()
        ids = [f.id() for f in layer.getFeatures()]
        if ids:
            prov.deleteFeatures(ids)

        feats = []
        xform = None
        try:
            src = QgsCoordinateReferenceSystem("EPSG:4326")
            dst = layer.crs()
            if src != dst:
                xform = QgsCoordinateTransform(src, dst, QgsProject.instance())
        except Exception:
            xform = None

        for scene in self._all_scenes:
            sid = str(scene.get("id", ""))
            if sid not in self._checked_ids:
                continue

            geom = self._stac_geometry_to_qgs(scene.get("geometry"))
            if geom is None or geom.isEmpty():
                continue
            if xform is not None:
                try:
                    geom.transform(xform)
                except Exception:
                    continue

            props = scene.get("properties", {}) or {}
            feat = QgsFeature(layer.fields())
            feat.setGeometry(geom)
            feat.setAttributes([
                sid,
                str(props.get("datetime", "")),
                float(props.get("eo:cloud_cover", -1) or -1),
                str(scene.get("collection", "")),
            ])
            feats.append(feat)

        if feats:
            prov.addFeatures(feats)
        layer.updateExtents()
        layer.triggerRepaint()
        self._zoom_to_footprints()

    def _zoom_to_footprints(self):
        try:
            ext = self._layer.extent()
            if ext is None or ext.isEmpty():
                self.canvas.refresh()
                return
            ext.scale(1.1)
            self.canvas.setExtent(ext)
            self.canvas.refresh()
        except Exception:
            try:
                self.canvas.refresh()
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

    def _populate_table(self):
        self.table.setRowCount(0)
        self.table.blockSignals(True)

        for scene in self._all_scenes:
            row = self.table.rowCount()
            self.table.insertRow(row)

            sid = str(scene.get("id", ""))
            props = scene.get("properties", {}) or {}

            id_item = QTableWidgetItem(sid)
            id_item.setData(Qt.UserRole, sid)
            dt_item = QTableWidgetItem(str(props.get("datetime", "")))
            cloud_item = QTableWidgetItem(str(props.get("eo:cloud_cover", "")))

            self.table.setItem(row, 0, id_item)
            self.table.setItem(row, 1, dt_item)
            self.table.setItem(row, 2, cloud_item)

        self.table.blockSignals(False)
        self.table.resizeColumnsToContents()
        self._select_all_scenes()

    def _select_all_scenes(self):
        self.table.blockSignals(True)
        self.table.selectAll()
        self.table.blockSignals(False)
        self._on_table_selection_changed()

    def _deselect_all_scenes(self):
        self.table.blockSignals(True)
        self.table.clearSelection()
        self.table.blockSignals(False)
        self._on_table_selection_changed()

    def _on_table_selection_changed(self):
        rows = self.table.selectionModel().selectedRows()
        selected_ids = set()
        for idx in rows:
            row = idx.row()
            row_item = self.table.item(row, 0)
            if not row_item:
                continue
            sid = str(row_item.data(Qt.UserRole) or row_item.text() or "")
            if sid:
                selected_ids.add(sid)

        self._checked_ids = selected_ids
        self._reload_layer_features(self._layer)
        self.canvas.refresh()

        if not rows:
            self.detailText.setPlainText("No details available.")
            return
        row = rows[0].row()
        item = self.table.item(row, 0)
        if not item:
            return
        sid = str(item.data(Qt.UserRole) or item.text() or "")
        self._show_scene_details(sid)

    def _on_feature_identified(self, feature):
        sid = str(feature.attribute("scene_id") or "")
        if sid:
            self._show_scene_details(sid)

    def _show_scene_details(self, scene_id: str):
        scene = self._scene_by_id.get(scene_id)
        if not scene:
            self.detailText.setPlainText("No details available.")
            return

        props = scene.get("properties", {}) or {}
        lines = [
            f"Scene ID: {scene.get('id', '')}",
            f"Collection: {scene.get('collection', '')}",
            f"Datetime: {props.get('datetime', '')}",
            f"Cloud cover: {props.get('eo:cloud_cover', '')}",
            "",
            "Other properties:",
        ]

        for k in sorted(props.keys()):
            if k in ("datetime", "eo:cloud_cover"):
                continue
            lines.append(f"- {k}: {props.get(k)}")

        self.detailText.setPlainText("\n".join(lines))
