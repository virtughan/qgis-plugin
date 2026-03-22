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
    QgsWkbTypes,
    QgsGeometry,
    QgsPointXY,
    QgsVectorLayer,
    QgsFeature,
    QgsField,
)
from qgis.gui import QgsMapCanvas, QgsMapTool, QgsRubberBand


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
            sym.symbolLayer(0).setStrokeWidth(2)  # Ensure stroke width matches drawing tool
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
        self.rb = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        self.rb.setWidth(2)
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
        if e.button() == Qt.LeftButton:
            self.points.append(self.toMapCoordinates(e.pos()))
        elif e.button() == Qt.RightButton:
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
        if e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self._finish()
        elif e.key() == Qt.Key_Escape:
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
            self.rb.reset(QgsWkbTypes.PolygonGeometry)
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
        self.rb = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        self.rb.setWidth(2)
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
        if e.button() == Qt.LeftButton:
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
        if e.button() == Qt.LeftButton and self.start_pt is not None:
            cur = self.toMapCoordinates(e.pos())
            xmin = min(self.start_pt.x(), cur.x()); xmax = max(self.start_pt.x(), cur.x())
            ymin = min(self.start_pt.y(), cur.y()); ymax = max(self.start_pt.y(), cur.y())
            rect = QgsRectangle(xmin, ymin, xmax, ymax)
            self._finish(None if rect.isEmpty() else rect)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self._finish(None)

    def _finish(self, rect: QgsRectangle | None):
        try:
            self.rb.reset(QgsWkbTypes.PolygonGeometry)
        except Exception:
            pass
        try:
            self.canvas.unsetMapTool(self)
        except Exception:
            pass
        self.on_done(rect)
