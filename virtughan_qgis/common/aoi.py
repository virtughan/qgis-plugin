"""
Reusable AOI (Area of Interest) helpers:
- AoiManager: creates/persists a single-feature memory layer for the AOI
- AoiPolygonTool: freehand polygon draw tool (left-click add, right/double/Enter finish)
- AoiRectTool: press-drag-release rectangle tool
- rect_to_wgs84_bbox / geom_to_wgs84_bbox: utilities to get WGS84 bbox
"""

from qgis.PyQt.QtCore import Qt, QVariant
from qgis.PyQt.QtGui import QColor
from qgis.core import (
    QgsProject,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsRectangle,
    QgsGeometry,
    QgsPointXY,
    QgsVectorLayer,
    QgsFeature,
    QgsField,
    QgsWkbTypes,
)
from qgis.gui import QgsMapCanvas, QgsMapTool, QgsRubberBand

from ..qt_compat import QtCompat, get_polygon_geometry_type


def rect_to_wgs84_bbox(rect: QgsRectangle, project: QgsProject) -> list[float]:
    src = project.crs()
    dst = QgsCoordinateReferenceSystem("EPSG:4326")
    xf = QgsCoordinateTransform(src, dst, project)
    r = xf.transformBoundingBox(rect)
    return [r.xMinimum(), r.yMinimum(), r.xMaximum(), r.yMaximum()]


def geom_to_wgs84_bbox(geom: QgsGeometry, project: QgsProject) -> list[float]:
    g = QgsGeometry(geom)  # clone
    g.transform(QgsCoordinateTransform(project.crs(), QgsCoordinateReferenceSystem("EPSG:4326"), project))
    r = g.boundingBox()
    return [r.xMinimum(), r.yMinimum(), r.xMaximum(), r.yMaximum()]


def bbox_area_km2(bbox: list[float] | tuple[float, float, float, float] | None) -> float:
    if not bbox or len(bbox) != 4:
        return 0.0
    x1, y1, x2, y2 = [float(v) for v in bbox]
    mid_lat = (y1 + y2) / 2.0
    km_per_deg_lat = 110.574
    km_per_deg_lon = 111.320
    try:
        import math
        km_per_deg_lon *= max(0.05, math.cos(math.radians(mid_lat)))
    except Exception:
        pass
    return abs((x2 - x1) * km_per_deg_lon) * abs((y2 - y1) * km_per_deg_lat)


def aoi_size_level(bbox, *, large_km2: float = 2500.0, very_large_km2: float = 10000.0) -> tuple[str, float]:
    area = bbox_area_km2(bbox)
    if area >= very_large_km2:
        return "very_large", area
    if area >= large_km2:
        return "large", area
    return "normal", area


def polygon_layers(project: QgsProject | None = None):
    project = project or QgsProject.instance()
    layers = []
    try:
        for layer in project.mapLayers().values():
            if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
                continue
            try:
                geom_type = QgsWkbTypes.geometryType(layer.wkbType())
            except Exception:
                continue
            if geom_type == QgsWkbTypes.PolygonGeometry:
                layers.append(layer)
    except Exception:
        pass
    return layers


def feature_geometry_in_project_crs(layer: QgsVectorLayer, feature: QgsFeature, project: QgsProject | None = None):
    project = project or QgsProject.instance()
    geom = feature.geometry()
    if geom is None or geom.isEmpty():
        return None
    geom = QgsGeometry(geom)
    try:
        src = layer.crs()
        dst = project.crs()
        if src.isValid() and dst.isValid() and src.authid() != dst.authid():
            geom.transform(QgsCoordinateTransform(src, dst, project))
    except Exception:
        pass
    return geom


def combined_feature_geometry_in_project_crs(layer: QgsVectorLayer, features, project: QgsProject | None = None):
    geoms = []
    for feature in features or []:
        geom = feature_geometry_in_project_crs(layer, feature, project)
        if geom is not None and not geom.isEmpty():
            geoms.append(geom)
    if not geoms:
        return None
    combined = QgsGeometry(geoms[0])
    for geom in geoms[1:]:
        try:
            combined = combined.combine(geom)
        except Exception:
            try:
                combined = combined.combine(QgsGeometry(geom))
            except Exception:
                pass
    return combined


class AoiManager:
    """
    Keeps exactly one AOI feature in a temporary memory layer.
    Use replace_geometry() on every draw. Use clear() to remove the layer.
    """
    def __init__(self, iface, layer_name: str = "AOI (drawn)", fill_color: QColor = None, stroke_color: QColor = None):
        self.iface = iface
        self.layer = None
        self.layer_name = layer_name
        # Default colors (blue)
        self.fill_color = fill_color or QColor(0, 102, 255, 60)
        self.stroke_color = stroke_color or QColor(0, 102, 255, 200)

    def ensure_layer(self):
        if self.layer and self.layer.isValid():
            return self.layer
        crs = self.iface.mapCanvas().mapSettings().destinationCrs()
        self.layer = QgsVectorLayer(f"Polygon?crs={crs.authid()}", self.layer_name, "memory")
        prov = self.layer.dataProvider()
        prov.addAttributes([QgsField("id", QVariant.Int), QgsField("label", QVariant.String)])
        self.layer.updateFields()
        QgsProject.instance().addMapLayer(self.layer)
        # Apply stored colors to symbol
        try:
            sym = self.layer.renderer().symbol()
            sym.setColor(self.fill_color)
            sym.symbolLayer(0).setStrokeColor(self.stroke_color)
            sym.symbolLayer(0).setStrokeWidth(0.5)  # Final AOI layer stroke thickness
            self.layer.triggerRepaint()
            # Force legend refresh
            self.layer.emitStyleChanged()
        except Exception:
            pass
        return self.layer

    def replace_geometry(self, geom_map: QgsGeometry):
        lyr = self.ensure_layer()
        prov = lyr.dataProvider()
        ids = [f.id() for f in lyr.getFeatures()]
        if ids:
            prov.deleteFeatures(ids)
        feat = QgsFeature(lyr.fields())
        feat.setGeometry(geom_map)
        feat.setAttributes([1, "AOI"])
        prov.addFeatures([feat])
        lyr.updateExtents()
        lyr.triggerRepaint()

    def clear(self):
        if self.layer and self.layer.isValid():
            try:
                QgsProject.instance().removeMapLayer(self.layer.id())
            except Exception:
                pass
        self.layer = None
        # Refresh canvas to immediately show the removed layer
        try:
            self.iface.mapCanvas().refresh()
        except Exception:
            pass


class AoiPolygonTool(QgsMapTool):
    """Polygon drawing tool: left-click add, right-click/double-click/Enter to finish."""
    def __init__(self, canvas: QgsMapCanvas, on_done, stroke_color: QColor = None, fill_color: QColor = None):
        super().__init__(canvas)
        self.canvas = canvas
        self.on_done = on_done
        self.points = []
        self.rb = QgsRubberBand(canvas, get_polygon_geometry_type())
        self.rb.setWidth(1)
        # Use provided colors or default blue
        stroke = stroke_color or QColor(0, 102, 255, 200)
        fill = fill_color or QColor(0, 102, 255, 60)
        try:
            self.rb.setColor(stroke)
            self.rb.setFillColor(fill)
        except Exception:
            try:
                self.rb.setStrokeColor(stroke)
            except Exception:
                pass

    def canvasPressEvent(self, e):
        if e.button() == QtCompat.LeftButton:
            self.points.append(self.toMapCoordinates(e.pos()))
        elif e.button() == QtCompat.RightButton:
            self._finish()

    def canvasMoveEvent(self, e):
        if not self.points:
            return
        temp = self.points + [self.toMapCoordinates(e.pos())]
        geom = QgsGeometry.fromPolygonXY([list(map(QgsPointXY, temp))])
        self.rb.setToGeometry(geom, None)

    def canvasDoubleClickEvent(self, e):
        self._finish()

    def keyPressEvent(self, e):
        if e.key() in (QtCompat.Key_Return, QtCompat.Key_Enter):
            self._finish()
        elif e.key() == QtCompat.Key_Escape:
            self._cleanup()
            self.on_done(None)

    def _finish(self):
        poly = None
        if len(self.points) >= 3:
            ring = list(map(QgsPointXY, self.points + [self.points[0]]))
            poly = QgsGeometry.fromPolygonXY([ring])
        self._cleanup()
        self.on_done(poly)

    def _cleanup(self):
        try:
            self.rb.reset(get_polygon_geometry_type())
        except Exception:
            pass
        self.points.clear()
        try:
            self.canvas.unsetMapTool(self)
        except Exception:
            pass


class AoiRectTool(QgsMapTool):
    """Press-drag-release rectangle tool."""
    def __init__(self, canvas: QgsMapCanvas, on_done, stroke_color: QColor = None, fill_color: QColor = None):
        super().__init__(canvas)
        self.canvas = canvas
        self.on_done = on_done
        self.start_pt = None
        self.rb = QgsRubberBand(canvas, get_polygon_geometry_type())
        self.rb.setWidth(1)
        # Use provided colors or default blue
        stroke = stroke_color or QColor(0, 102, 255, 200)
        fill = fill_color or QColor(0, 102, 255, 60)
        try:
            self.rb.setColor(stroke)
            self.rb.setFillColor(fill)
        except Exception:
            try:
                self.rb.setStrokeColor(stroke)
            except Exception:
                pass

    def canvasPressEvent(self, e):
        if e.button() == QtCompat.LeftButton:
            self.start_pt = self.toMapCoordinates(e.pos())

    def canvasMoveEvent(self, e):
        if self.start_pt is None:
            return
        cur = self.toMapCoordinates(e.pos())
        xmin = min(self.start_pt.x(), cur.x()); xmax = max(self.start_pt.x(), cur.x())
        ymin = min(self.start_pt.y(), cur.y()); ymax = max(self.start_pt.y(), cur.y())
        rect = QgsRectangle(xmin, ymin, xmax, ymax)
        ring = [
            QgsPointXY(rect.xMinimum(), rect.yMinimum()),
            QgsPointXY(rect.xMinimum(), rect.yMaximum()),
            QgsPointXY(rect.xMaximum(), rect.yMaximum()),
            QgsPointXY(rect.xMaximum(), rect.yMinimum()),
            QgsPointXY(rect.xMinimum(), rect.yMinimum()),
        ]
        self.rb.setToGeometry(QgsGeometry.fromPolygonXY([ring]), None)

    def canvasReleaseEvent(self, e):
        if e.button() == QtCompat.LeftButton and self.start_pt is not None:
            cur = self.toMapCoordinates(e.pos())
            xmin = min(self.start_pt.x(), cur.x()); xmax = max(self.start_pt.x(), cur.x())
            ymin = min(self.start_pt.y(), cur.y()); ymax = max(self.start_pt.y(), cur.y())
            rect = QgsRectangle(xmin, ymin, xmax, ymax)
            self._finish(None if rect.isEmpty() else rect)

    def keyPressEvent(self, e):
        if e.key() == QtCompat.Key_Escape:
            self._finish(None)

    def _finish(self, rect: QgsRectangle | None):
        try:
            self.rb.reset(get_polygon_geometry_type())
        except Exception:
            pass
        try:
            self.canvas.unsetMapTool(self)
        except Exception:
            pass
        self.on_done(rect)
