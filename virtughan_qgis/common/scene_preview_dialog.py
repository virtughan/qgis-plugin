from qgis.PyQt.QtCore import Qt, QVariant
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qgis.core import (
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsVectorLayer,
)
from qgis.gui import QgsMapCanvas, QgsMapToolIdentifyFeature


class ScenePreviewDialog(QDialog):
    def __init__(self, parent, scenes, title, fill_color: QColor, stroke_color: QColor):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(980, 680)

        self._all_scenes = scenes or []
        self._scene_by_id = {str(s.get("id", "")): s for s in self._all_scenes}
        self._checked_ids = set(self._scene_by_id.keys())
        self._table_populating = False

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        info = QLabel("Top list controls footprint visibility in this preview map. Click a footprint to inspect attributes.")
        root.addWidget(info)

        top_split = QSplitter(Qt.Horizontal, self)
        root.addWidget(top_split, 2)

        table_host = QWidget(top_split)
        table_layout = QVBoxLayout(table_host)
        table_layout.setContentsMargins(0, 0, 0, 0)

        self.table = QTableWidget(table_host)
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Show", "Scene ID", "Datetime", "Cloud %"])
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.itemChanged.connect(self._on_table_item_changed)
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

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        self.buttons.button(QDialogButtonBox.Ok).setText("Apply Selection")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        root.addWidget(self.buttons)

        self._layer = self._build_preview_layer(fill_color, stroke_color)
        self.canvas.setLayers([self._layer])
        self.canvas.zoomToFullExtent()

        self._identify_tool = QgsMapToolIdentifyFeature(self.canvas, self._layer)
        self._identify_tool.featureIdentified.connect(self._on_feature_identified)
        self.canvas.setMapTool(self._identify_tool)

        self._populate_table()

    def selected_scenes(self):
        selected = []
        for scene in self._all_scenes:
            sid = str(scene.get("id", ""))
            if sid in self._checked_ids:
                selected.append(scene)
        return selected

    def _build_preview_layer(self, fill_color: QColor, stroke_color: QColor):
        layer = QgsVectorLayer("Polygon?crs=EPSG:4326", "Scene Preview", "memory")
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

    def _reload_layer_features(self, layer):
        prov = layer.dataProvider()
        ids = [f.id() for f in layer.getFeatures()]
        if ids:
            prov.deleteFeatures(ids)

        feats = []
        for scene in self._all_scenes:
            sid = str(scene.get("id", ""))
            if sid not in self._checked_ids:
                continue

            geom = self._stac_geometry_to_qgs(scene.get("geometry"))
            if geom is None or geom.isEmpty():
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
        self._table_populating = True
        self.table.setRowCount(0)

        for scene in self._all_scenes:
            row = self.table.rowCount()
            self.table.insertRow(row)

            sid = str(scene.get("id", ""))
            props = scene.get("properties", {}) or {}

            check_item = QTableWidgetItem()
            check_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
            check_item.setCheckState(Qt.Checked if sid in self._checked_ids else Qt.Unchecked)
            check_item.setData(Qt.UserRole, sid)

            id_item = QTableWidgetItem(sid)
            id_item.setData(Qt.UserRole, sid)
            dt_item = QTableWidgetItem(str(props.get("datetime", "")))
            cloud_item = QTableWidgetItem(str(props.get("eo:cloud_cover", "")))

            self.table.setItem(row, 0, check_item)
            self.table.setItem(row, 1, id_item)
            self.table.setItem(row, 2, dt_item)
            self.table.setItem(row, 3, cloud_item)

        self.table.resizeColumnsToContents()
        self._table_populating = False

    def _on_table_item_changed(self, item):
        if self._table_populating:
            return
        if item.column() != 0:
            return

        sid = str(item.data(Qt.UserRole) or "")
        if not sid:
            return

        if item.checkState() == Qt.Checked:
            self._checked_ids.add(sid)
        else:
            self._checked_ids.discard(sid)

        self._reload_layer_features(self._layer)
        self.canvas.refresh()

    def _on_table_selection_changed(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        row = rows[0].row()
        item = self.table.item(row, 1)
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
