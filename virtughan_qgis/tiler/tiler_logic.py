from typing import Optional
from urllib.parse import urlencode, quote

from qgis.core import QgsProject, QgsRasterLayer, QgsMessageLog, Qgis


class TilerLogic:
    """Create/register an XYZ tile layer that proxies to your FastAPI tiler."""
    TILER_PATH = "/tile/{z}/{x}/{y}"  

    def __init__(self, iface):
        self.iface = iface

    def _build_query(self, params: dict) -> str:
        from urllib.parse import urlencode, quote
        clean = {}
        for k, v in params.items():
            if v is None or str(v) == "":
                continue
            # Formula needs special handling: the '+' operator gets mangled
            # by URL encoding/decoding (treated as space by some HTTP clients).
            # Replace '+' with '__PLUS__' placeholder, decoded on the server side.
            if k == "formula" and isinstance(v, str):
                import re
                v = re.sub(r'\s*([+\-*/()])\s*', r'\1', v).strip()
                v = v.replace("+", "__PLUS__")
            clean[k] = v
        return urlencode(clean, doseq=True, quote_via=quote, safe="()/_-")


    def build_xyz_uri(self, backend_url: str, name: str, params: dict) -> str:
        backend_url = backend_url.rstrip("/")
        base = f"{backend_url}{self.TILER_PATH}"
        qs = self._build_query(params)
        url_template = f"{base}?{qs}" if qs else base

        
        
        
        url_value = url_template.replace("&", "%26")

        
        provider_uri = f"type=xyz&zmin=10&zmax=23&url={url_value}"
        return provider_uri


    def add_xyz_layer(self, backend_url: str, name: str, params: dict):
        uri = self.build_xyz_uri(backend_url, name, params)
        QgsMessageLog.logMessage(f"[VirtuGhan Tiler] URI: {uri}", "VirtuGhan", Qgis.Info)
        layer = QgsRasterLayer(uri, name, "wms")  
        if not layer.isValid():
            raise RuntimeError("Failed to create XYZ layer. Check URL/params.")
        QgsProject.instance().addMapLayer(layer)
        return layer

    @staticmethod
    def default_params(
        start_date: str,
        end_date: str,
        cloud_cover: int,
        band1: str,
        band2: str,
        formula: str,
        bands: Optional[list[str]] = None,
        timeseries: bool = False,
        operation: Optional[str] = None,
        collection: str = "sentinel-2-l2a",
        colormap_str: str = "RdYlGn",
    ) -> dict:
        base = {
            "start_date": start_date,
            "end_date": end_date,
            "cloud_cover": cloud_cover,
            "band1": band1,
            "band2": band2 or "",
            "bands": list(bands or [b for b in (band1, band2) if b]),
            "formula": formula,
            "collection": collection,
            "colormap_str": colormap_str or "RdYlGn",
        }
        if timeseries:
            base["timeseries"] = True
            base["operation"] = operation or "median"
        return base
