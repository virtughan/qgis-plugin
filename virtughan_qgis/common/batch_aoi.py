from __future__ import annotations

import os
import re

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsGeometry,
    QgsProject,
    QgsVectorLayer,
)

from .aoi import feature_geometry_in_project_crs, geom_to_wgs84_bbox


class AoiTransformError(RuntimeError):
    pass


def safe_batch_label(value, fallback: str) -> str:
    text = str(value or "").strip() or fallback
    text = re.sub(r"[^\w.-]+", "_", text, flags=re.UNICODE).strip("._-")
    return (text or fallback)[:80]


def feature_label(layer: QgsVectorLayer, feature: QgsFeature, index: int) -> str:
    fields = layer.fields()
    for name in ("name", "Name", "label", "Label", "id", "ID", "fid", "FID"):
        try:
            if fields.indexOf(name) >= 0:
                value = feature.attribute(name)
                if value not in (None, ""):
                    return safe_batch_label(value, f"feature_{index:03d}")
        except Exception:
            pass
    try:
        fid = feature.id()
    except Exception:
        fid = index
    return safe_batch_label(f"fid_{fid}", f"feature_{index:03d}")


def polygon_wgs84_coords(geom_project: QgsGeometry, project: QgsProject | None = None):
    project = project or QgsProject.instance()
    try:
        geom = QgsGeometry(geom_project)
        geom.transform(
            QgsCoordinateTransform(
                project.crs(),
                QgsCoordinateReferenceSystem("EPSG:4326"),
                project,
            )
        )
        poly = geom.asPolygon()
        if not poly:
            mp = geom.asMultiPolygon()
            ring = mp[0][0] if mp else []
        else:
            ring = poly[0]
        coords = [[float(p.x()), float(p.y())] for p in ring]
        return coords or None
    except Exception:
        return None


def _validate_crs(layer: QgsVectorLayer, project: QgsProject):
    if layer is None or not layer.isValid():
        raise AoiTransformError("Selected AOI layer is not valid.")
    if not layer.crs().isValid():
        raise AoiTransformError("Selected AOI layer has no valid CRS. Please define the layer CRS before using it as AOI.")
    if not project.crs().isValid():
        raise AoiTransformError("The QGIS project CRS is not valid. Please set a valid project CRS before using layer AOI.")


def _feature_geometry_in_project_crs_strict(layer: QgsVectorLayer, feature: QgsFeature, project: QgsProject):
    geom = feature.geometry()
    if geom is None or geom.isEmpty():
        raise AoiTransformError("Selected feature has empty geometry.")
    geom = QgsGeometry(geom)
    src = layer.crs()
    dst = project.crs()
    if src.isValid() and dst.isValid() and src.authid() != dst.authid():
        try:
            geom.transform(QgsCoordinateTransform(src, dst, project))
        except Exception as exc:
            raise AoiTransformError(f"Could not transform AOI layer geometry to the project CRS: {exc}") from exc
    if geom is None or geom.isEmpty():
        raise AoiTransformError("AOI geometry became empty after transformation to the project CRS.")
    return geom


def _bbox_wgs84_strict(geom_project: QgsGeometry, project: QgsProject):
    try:
        bbox = geom_to_wgs84_bbox(geom_project, project)
    except Exception as exc:
        raise AoiTransformError(f"Could not transform AOI to WGS84 (EPSG:4326), which is required by the satellite search backend: {exc}") from exc
    if not bbox or len(bbox) != 4:
        raise AoiTransformError("Could not create a WGS84 AOI bbox for the selected geometry.")
    try:
        x1, y1, x2, y2 = map(float, bbox)
    except Exception as exc:
        raise AoiTransformError(f"WGS84 AOI bbox contains invalid values: {bbox}") from exc
    if not (-180.0 <= x1 < x2 <= 180.0 and -90.0 <= y1 < y2 <= 90.0):
        raise AoiTransformError(f"Transformed AOI bbox is outside valid WGS84 bounds: {bbox}")
    return bbox


def specs_from_features(layer: QgsVectorLayer, features, project: QgsProject | None = None):
    project = project or QgsProject.instance()
    _validate_crs(layer, project)
    specs = []
    errors = []
    for index, feature in enumerate(features or [], start=1):
        try:
            geom = _feature_geometry_in_project_crs_strict(layer, feature, project)
            bbox = _bbox_wgs84_strict(geom, project)
        except AoiTransformError as exc:
            try:
                fid = feature.id()
            except Exception:
                fid = index
            errors.append(f"Feature {fid}: {exc}")
            continue
        try:
            fid = feature.id()
        except Exception:
            fid = index
        label = feature_label(layer, feature, index)
        specs.append(
            {
                "index": index,
                "fid": fid,
                "label": label,
                "folder_name": safe_batch_label(f"{index:03d}_{label}", f"feature_{index:03d}"),
                "geometry_project": geom,
                "bbox": bbox,
                "polygon_wgs84": polygon_wgs84_coords(geom, project),
            }
        )
    if errors:
        setattr(specs_from_features, "last_errors", errors)
    else:
        setattr(specs_from_features, "last_errors", [])
    return specs


def combined_geometry(specs):
    geoms = [s.get("geometry_project") for s in specs or [] if s.get("geometry_project")]
    if not geoms:
        return None
    geom = QgsGeometry(geoms[0])
    for other in geoms[1:]:
        try:
            geom = geom.combine(other)
        except Exception:
            pass
    return geom


def batch_root(output_base: str, prefix: str, run_id: str) -> str:
    return os.path.join(output_base, f"{prefix}_batch_{run_id}")
