
import re

from ..qt_compat import QgisCompat

DEFAULT_COLLECTION = "sentinel-2-l2a"

COLLECTION_LABELS = {
    "sentinel-2-l2a": "Sentinel-2",
    "landsat-c2-l2": "Landsat 8/9",
    "sentinel-1-rtc": "Sentinel-1 (Experimental)",
}

COLLECTION_ORDER = ["sentinel-2-l2a", "landsat-c2-l2", "sentinel-1-rtc"]

FALLBACK_COLLECTION_BANDS = {
    "sentinel-2-l2a": {
        "red": {"gsd": 10, "description": "Red, Band 4"},
        "green": {"gsd": 10, "description": "Green, Band 3"},
        "blue": {"gsd": 10, "description": "Blue, Band 2"},
        "nir": {"gsd": 10, "description": "Near-Infrared, Band 8"},
        "swir22": {"gsd": 20, "description": "Short-Wave Infrared 2, Band 12"},
        "rededge2": {"gsd": 20, "description": "Red Edge 2, Band 6"},
        "rededge3": {"gsd": 20, "description": "Red Edge 3, Band 7"},
        "rededge1": {"gsd": 20, "description": "Red Edge 1, Band 5"},
        "swir16": {"gsd": 20, "description": "Short-Wave Infrared 1, Band 11"},
        "wvp": {"gsd": 20, "description": "Water Vapour, Band 9"},
        "nir08": {"gsd": 20, "description": "Near-Infrared Narrow, Band 8A"},
        "aot": {"gsd": 20, "description": "Aerosol Optical Thickness"},
        "coastal": {"gsd": 60, "description": "Coastal Aerosol, Band 1"},
        "nir09": {"gsd": 60, "description": "Water Vapour, Band 9"},
        "scl": {"gsd": 20, "description": "Scene Classification Layer"},
        "visual": {"gsd": 10, "description": "True Color Image (RGB)"},
    },
    "landsat-c2-l2": {
        "red": {"gsd": 30, "description": "Red, Band 4"},
        "green": {"gsd": 30, "description": "Green, Band 3"},
        "blue": {"gsd": 30, "description": "Blue, Band 2"},
        "nir08": {"gsd": 30, "description": "Near-Infrared, Band 5"},
        "swir16": {"gsd": 30, "description": "Short-Wave Infrared 1, Band 6"},
        "swir22": {"gsd": 30, "description": "Short-Wave Infrared 2, Band 7"},
        "coastal": {"gsd": 30, "description": "Coastal Aerosol, Band 1"},
        "lwir11": {"gsd": 100, "description": "Thermal Infrared, Band 10"},
    },
    "sentinel-1-rtc": {
        "vv": {"gsd": 10, "description": "VV polarization"},
        "vh": {"gsd": 10, "description": "VH polarization"},
        "hh": {"gsd": 10, "description": "HH polarization"},
        "hv": {"gsd": 10, "description": "HV polarization"},
    },
}


def collection_choices():
    return [(collection_id, COLLECTION_LABELS.get(collection_id, collection_id)) for collection_id in COLLECTION_ORDER]


def collection_label(collection_id):
    return COLLECTION_LABELS.get(collection_id or DEFAULT_COLLECTION, collection_id or DEFAULT_COLLECTION)


def normalize_collection(collection_id):
    value = (collection_id or "").strip()
    return value if value in COLLECTION_ORDER else DEFAULT_COLLECTION


def sentinel1_extra_query(mode=None):
    mode_value = (mode or "").strip().upper()
    if mode_value in {"IW", "EW", "SM", "WV"}:
        return {"sar:instrument_mode": {"eq": mode_value}}
    return {"sar:instrument_mode": {"eq": "IW"}}


def extra_query_for_collection(collection_id):
    collection = normalize_collection(collection_id)
    if collection == "sentinel-1-rtc":
        return sentinel1_extra_query("IW")
    return None


def collection_band_metadata(collection_id=DEFAULT_COLLECTION):
    collection = normalize_collection(collection_id)
    try:
        from virtughan.collections import get_collection
        cfg = get_collection(collection)
        meta = {}
        for name, info in cfg.bands.items():
            meta[name] = {
                "gsd": getattr(info, "resolution", None),
                "description": getattr(info, "description", ""),
                "wavelength": getattr(info, "wavelength", ""),
            }
        if meta:
            return meta
    except Exception:  # nosec B110 - defensive QGIS cleanup or optional API fallback.
        pass
    return dict(FALLBACK_COLLECTION_BANDS.get(collection, FALLBACK_COLLECTION_BANDS[DEFAULT_COLLECTION]))


def collection_band_names(collection_id=DEFAULT_COLLECTION):
    return list(collection_band_metadata(collection_id).keys())


def index_presets_two_band(collection_id=DEFAULT_COLLECTION):
    """
    Return list of 6 preset two-band spectral indices for Earth observation.
    Each dict contains: label, band1, band2, formula.
    """
    collection = normalize_collection(collection_id)
    if collection == "sentinel-1-rtc":
        return [
            {
                "label": "VV",
                "band1": "vv",
                "band2": "",
                "formula": "vv",
            },
            {
                "label": "VH",
                "band1": "vh",
                "band2": "",
                "formula": "vh",
            },
            {
                "label": "VV/VH dB",
                "band1": "vv",
                "band2": "vh",
                "formula": "10 * log10(vv / vh)",
            },
            {
                "label": "VH/VV",
                "band1": "vh",
                "band2": "vv",
                "formula": "vh / vv",
            },
        ]

    nir_band = "nir" if collection == "sentinel-2-l2a" else "nir08"
    return [
        {
            "label": "NDVI",
            "band1": "red",
            "band2": nir_band,
            "formula": f"({nir_band} - red) / ({nir_band} + red)"
        },
        {
            "label": "NDWI",
            "band1": nir_band,
            "band2": "swir16",
            "formula": f"({nir_band} - swir16) / ({nir_band} + swir16)"
        },
        {
            "label": "NDBI",
            "band1": "swir16",
            "band2": nir_band,
            "formula": f"(swir16 - {nir_band}) / (swir16 + {nir_band})"
        },
        {
            "label": "NDMI",
            "band1": nir_band,
            "band2": "swir16",
            "formula": f"({nir_band} - swir16) / ({nir_band} + swir16)"
        },
        {
            "label": "GNDVI",
            "band1": "green",
            "band2": nir_band,
            "formula": f"({nir_band} - green) / ({nir_band} + green)"
        },
        {
            "label": "SAVI",
            "band1": "red",
            "band2": nir_band,
            "formula": f"1.5 * ({nir_band} - red) / ({nir_band} + red + 0.5)"
        },
    ]

def get_index_preset(label, collection_id=DEFAULT_COLLECTION):
    """
    Get preset dict by label (case-insensitive lookup).
    Returns a copy of the preset dict, or None if not found.
    """
    wanted = (label or "").strip().lower()
    for preset in index_presets_two_band(collection_id):
        if preset.get("label", "").strip().lower() == wanted:
            return dict(preset)
    return None

def match_index_preset(band1, band2, formula, collection_id=DEFAULT_COLLECTION):
    """
    Reverse-lookup: find preset label that matches given band1, band2, formula.
    Comparison is case-insensitive and whitespace-insensitive.
    Returns preset label (str) or None if no match found.
    """
    b1 = (band1 or "").strip().lower()
    b2 = (band2 or "").strip().lower()
    fx = "".join((formula or "").split()).lower()

    for preset in index_presets_two_band(collection_id):
        p1 = (preset.get("band1") or "").strip().lower()
        p2 = (preset.get("band2") or "").strip().lower()
        pf = "".join((preset.get("formula") or "").split()).lower()

        if b1 == p1 and b2 == p2 and fx == pf:
            return preset.get("label")

    return None
import os, json

from qgis.core import Qgis, QgsMessageLog

def load_bands_meta(collection_id=DEFAULT_COLLECTION):
    """
    Try vendored JSON first, else package resource via importlib.resources.
    Returns dict or None.
    """
    
    collection = normalize_collection(collection_id)
    if collection != DEFAULT_COLLECTION:
        return collection_band_metadata(collection)

    here = os.path.dirname(__file__)
    vendored = os.path.join(os.path.dirname(here), "libs", "virtughan", "data", "sentinel-2-bands.json")
    if os.path.exists(vendored):
        try:
            with open(vendored, "r") as f:
                return json.load(f)
        except Exception:  # nosec B110 - defensive QGIS cleanup or optional API fallback.
            pass

    
    try:
        import importlib.resources as resources
        with resources.as_file(resources.files("virtughan").joinpath("data/sentinel-2-bands.json")) as p:
            if p.exists():
                with open(p, "r") as f:
                    return json.load(f)
    except Exception:  # nosec B110 - defensive QGIS cleanup or optional API fallback.
        pass

    QgsMessageLog.logMessage("sentinel-2-bands.json not found; falling back to default band list.", "VirtuGhan", QgisCompat.Warning)
    return None

def default_band_list():
    return collection_band_names(DEFAULT_COLLECTION)

def populate_band_combos(band1_combo, band2_combo, bands_meta=None):
    bands = list(bands_meta.keys()) if bands_meta else default_band_list()
    band1_combo.clear(); band2_combo.clear()
    band1_combo.addItems(bands)
    band2_combo.addItems([""] + bands)  


def build_bands_list(band1, band2=None, formula=None):
    bands = []
    if isinstance(band1, (list, tuple)):
        candidates = list(band1)
    else:
        candidates = [band1, band2]
    for band in candidates:
        value = (band or "").strip()
        if value and value not in bands:
            bands.append(value)
    return bands


def filter_bands_used_by_formula(bands, formula):
    """Return selected bands that appear as standalone names in formula."""
    formula_text = formula or ""
    used = []
    for band in bands or []:
        value = (band or "").strip()
        if not value or value in used:
            continue
        pattern = rf"(?<![A-Za-z0-9_]){re.escape(value)}(?![A-Za-z0-9_])"
        if re.search(pattern, formula_text):
            used.append(value)
    return used


def processor_kwargs_from_params(params, *, log_file=None, workers=None):
    band1 = params.get("band1")
    band2 = params.get("band2")
    bands = params.get("bands") or build_bands_list(band1, band2, params.get("formula"))
    kwargs = {
        "bbox": params["bbox"],
        "start_date": params["start_date"],
        "end_date": params["end_date"],
        "cloud_cover": params["cloud_cover"],
        "formula": params["formula"],
        "bands": bands,
        "operation": params["operation"],
        "timeseries": params["timeseries"],
        "output_dir": params["output_dir"],
        "cmap": "RdYlGn",
        "workers": int(workers if workers is not None else params.get("workers", 1) or 1),
        "smart_filter": params["smart_filter"],
        "collection": normalize_collection(params.get("collection")),
        "extra_query": params.get("extra_query"),
    }
    if log_file is not None:
        kwargs["log_file"] = log_file
    return kwargs


def extract_kwargs_from_params(params, *, log_file=None, workers=None):
    kwargs = {
        "bbox": params["bbox"],
        "start_date": params["start_date"],
        "end_date": params["end_date"],
        "cloud_cover": params["cloud_cover"],
        "bands_list": list(params["bands_list"]),
        "output_dir": params["output_dir"],
        "workers": int(workers if workers is not None else params.get("workers", 1) or 1),
        "zip_output": params.get("zip_output", False),
        "smart_filter": params.get("smart_filter", True),
        "collection": normalize_collection(params.get("collection")),
        "extra_query": params.get("extra_query"),
    }
    if log_file is not None:
        kwargs["log_file"] = log_file
    if params.get("polygon_wgs84"):
        kwargs["polygon_wgs84"] = params["polygon_wgs84"]
    return kwargs


def search_stac_features(collection_id, bbox, start_date, end_date, cloud_cover, extra_query=None):
    collection = normalize_collection(collection_id)
    try:
        from virtughan_qgis.bootstrap import activate_runtime_paths, purge_non_runtime_modules
        activate_runtime_paths()
        purge_non_runtime_modules((
            "attr",
            "attrs",
            "pystac",
            "pystac_client",
            "planetary_computer",
            "jsonschema",
            "referencing",
            "rpds",
            "matplotlib",
        ))
    except Exception:  # nosec B110 - defensive QGIS cleanup or optional API fallback.
        pass
    from virtughan.collections import get_collection
    from virtughan.stac import search_stac
    config = get_collection(collection)
    return search_stac(
        config,
        bbox,
        start_date,
        end_date,
        None if collection == "sentinel-1-rtc" else cloud_cover,
        extra_query=extra_query,
    )

def check_resolution_warning(bands_meta, band1, band2):
    """
    Return a warning string if GSD differs, else None.
    """
    if not bands_meta or not band1 or not band2 or band1 == band2:
        return None
    g1 = bands_meta.get(band1, {}).get("gsd")
    g2 = bands_meta.get(band2, {}).get("gsd")
    if g1 and g2 and g1 != g2:
        return f"Band resolution mismatch: {band1}={g1}m, {band2}={g2}m."
    return None

def auto_workers():
    try:
        import multiprocessing
        return max(1, multiprocessing.cpu_count() - 1)
    except Exception:  # nosec B110 - defensive QGIS cleanup or optional API fallback.
        return 1

def qdate_to_iso(qdate):
    return qdate.toString("yyyy-MM-dd")
