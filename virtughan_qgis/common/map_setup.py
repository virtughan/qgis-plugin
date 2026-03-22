from qgis.core import (
    QgsProject, QgsRasterLayer, QgsCoordinateReferenceSystem,
    QgsCoordinateTransform, QgsRectangle
)
from qgis.gui import QgsMapCanvas
from qgis.PyQt.QtCore import QTimer

_OSM_URL = "type=xyz&url=https://tile.openstreetmap.org/{z}/{x}/{y}.png"
_OSM_NAME = "OpenStreetMap"


def _find_osm_layer():
    """Return the existing OSM XYZ layer if present, else None."""
    root = QgsProject.instance().layerTreeRoot()
    for lyr in QgsProject.instance().mapLayers().values():
        if isinstance(lyr, QgsRasterLayer) and lyr.providerType().lower() in ("wms", "wmsc", "xyz"):
            if "tile.openstreetmap.org" in (lyr.source() or "").lower():
                # Only treat it as existing basemap if it's in the layer tree
                # (temporary preview layers may be in registry but not in tree).
                if root.findLayer(lyr.id()) is not None:
                    return lyr
    return None


def has_osm_basemap() -> bool:
    return _find_osm_layer() is not None


def ensure_osm_basemap(name: str = _OSM_NAME,
                       as_bottom: bool = True,
                       set_project_crs: bool = True) -> QgsRasterLayer | None:
    """
    Ensure an OSM XYZ basemap exists in the project.
    - Adds it if missing.
    - Moves to bottom of layer tree if as_bottom=True.
    - Optionally sets project CRS to EPSG:3857 (good for web tiles).
    """
    prj = QgsProject.instance()
    root = prj.layerTreeRoot()

    lyr = _find_osm_layer()
    if lyr is None:
        lyr = QgsRasterLayer(_OSM_URL, name, "wms")  # QGIS handles 'type=xyz' via WMS provider
        if not lyr.isValid():
            return None
        prj.addMapLayer(lyr, False)
        # insert at bottom
        try:
            root.insertLayer(len(root.children()), lyr) if as_bottom else root.insertLayer(0, lyr)
        except Exception:
            root.addLayer(lyr)  # fallback (usually adds at top)
    else:
        if as_bottom:
            node = root.findLayer(lyr.id())
            if node:
                parent = node.parent() or root
                parent.removeChildNode(node)
                try:
                    root.insertLayer(len(root.children()), lyr)
                except Exception:
                    root.addLayer(lyr)

    if set_project_crs:
        try:
            prj.setCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
        except Exception:
            pass

    return lyr


def zoom_to_lonlat(iface,
                   lon: float,
                   lat: float,
                   scale_m: float = 10000.0,
                   delay_ms: int = 80):
    """
    Center the canvas on (lon, lat) in EPSG:4326 and set the scale (1:scale_m).
    A short delay avoids 'zoom to full extent' races while layers/CRS are settling.
    Note: scale_m is a *scale denominator* (e.g., 5000 => 1:5000), not meters.
    """
    canvas: QgsMapCanvas = iface.mapCanvas()
    if not canvas:
        return

    prj = QgsProject.instance()
    xform = QgsCoordinateTransform(
        QgsCoordinateReferenceSystem("EPSG:4326"),
        prj.crs(),
        prj.transformContext(),
    )
    pt = xform.transform(lon, lat)

    def _apply():
        canvas.setCenter(pt)
        try:
            canvas.zoomScale(scale_m)
        except Exception:
            pass
        # recenter again after zoom to keep the target pinned
        canvas.setCenter(pt)
        canvas.refresh()

    QTimer.singleShot(max(0, delay_ms), _apply)


def zoom_to_wgs84_bbox(iface,
                       xmin: float, ymin: float, xmax: float, ymax: float,
                       delay_ms: int = 80):
    """
    Zoom to a WGS84 bbox. Transforms to project CRS and sets canvas extent
    with a short delay to avoid being overwritten by extent resets.
    """
    canvas: QgsMapCanvas = iface.mapCanvas()
    if not canvas:
        return

    prj = QgsProject.instance()
    xform = QgsCoordinateTransform(
        QgsCoordinateReferenceSystem("EPSG:4326"),
        prj.crs(),
        prj.transformContext(),
    )
    rect = xform.transformBoundingBox(QgsRectangle(xmin, ymin, xmax, ymax))

    def _apply():
        canvas.setExtent(rect)
        canvas.refresh()

    QTimer.singleShot(max(0, delay_ms), _apply)


def setup_default_map(
    iface,
    center_wgs84: tuple[float, float] | None = None,
    scale_m: float = 10000.0,
    bbox_wgs84: tuple[float, float, float, float] | None = None,
    name: str = _OSM_NAME,
    set_project_crs: bool = True,
    *,
    skip_if_present: bool = False,
    skip_zoom_if_present: bool = True,
    zoom_delay_ms: int = 80,
):
    """
    One-shot convenience: add OSM (if missing) and zoom.
    If OSM already exists:
      - skip adding when skip_if_present=True,
      - skip zoom when skip_zoom_if_present=True.
    """
    exists = has_osm_basemap()
    if exists and skip_if_present:
        return

    ensure_osm_basemap(name=name, as_bottom=True, set_project_crs=set_project_crs)

    if exists and skip_zoom_if_present:
        return

    if bbox_wgs84:
        xmin, ymin, xmax, ymax = bbox_wgs84
        zoom_to_wgs84_bbox(iface, xmin, ymin, xmax, ymax, delay_ms=zoom_delay_ms)
    elif center_wgs84:
        lon, lat = center_wgs84
        zoom_to_lonlat(iface, lon, lat, scale_m=scale_m, delay_ms=zoom_delay_ms)
