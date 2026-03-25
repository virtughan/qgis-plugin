
def index_presets_two_band():
    """
    Return list of 6 preset two-band spectral indices for Earth observation.
    Each dict contains: label, band1, band2, formula.
    """
    return [
        {
            "label": "NDVI",
            "band1": "red",
            "band2": "nir",
            "formula": "(band2 - band1) / (band2 + band1)"
        },
        {
            "label": "NDWI",
            "band1": "nir",
            "band2": "swir16",
            "formula": "(band1 - band2) / (band1 + band2)"
        },
        {
            "label": "NDBI",
            "band1": "swir16",
            "band2": "nir",
            "formula": "(band1 - band2) / (band1 + band2)"
        },
        {
            "label": "NDMI",
            "band1": "nir",
            "band2": "swir16",
            "formula": "(band1 - band2) / (band1 + band2)"
        },
        {
            "label": "GNDVI",
            "band1": "green",
            "band2": "nir",
            "formula": "(band2 - band1) / (band2 + band1)"
        },
        {
            "label": "SAVI",
            "band1": "red",
            "band2": "nir",
            "formula": "1.5 * (band2 - band1) / (band2 + band1 + 0.5)"
        },
    ]

def get_index_preset(label):
    """
    Get preset dict by label (case-insensitive lookup).
    Returns a copy of the preset dict, or None if not found.
    """
    wanted = (label or "").strip().lower()
    for preset in index_presets_two_band():
        if preset.get("label", "").strip().lower() == wanted:
            return dict(preset)
    return None

def match_index_preset(band1, band2, formula):
    """
    Reverse-lookup: find preset label that matches given band1, band2, formula.
    Comparison is case-insensitive and whitespace-insensitive.
    Returns preset label (str) or None if no match found.
    """
    b1 = (band1 or "").strip().lower()
    b2 = (band2 or "").strip().lower()
    fx = "".join((formula or "").split()).lower()

    for preset in index_presets_two_band():
        p1 = (preset.get("band1") or "").strip().lower()
        p2 = (preset.get("band2") or "").strip().lower()
        pf = "".join((preset.get("formula") or "").split()).lower()

        if b1 == p1 and b2 == p2 and fx == pf:
            return preset.get("label")

    return None
import os, json

from qgis.core import Qgis, QgsMessageLog

def load_bands_meta():
    """
    Try vendored JSON first, else package resource via importlib.resources.
    Returns dict or None.
    """
    
    here = os.path.dirname(__file__)
    vendored = os.path.join(os.path.dirname(here), "libs", "virtughan", "data", "sentinel-2-bands.json")
    if os.path.exists(vendored):
        try:
            with open(vendored, "r") as f:
                return json.load(f)
        except Exception:
            pass

    
    try:
        import importlib.resources as resources
        with resources.as_file(resources.files("virtughan").joinpath("data/sentinel-2-bands.json")) as p:
            if p.exists():
                with open(p, "r") as f:
                    return json.load(f)
    except Exception:
        pass

    QgsMessageLog.logMessage("sentinel-2-bands.json not found; falling back to default band list.", "VirtuGhan", Qgis.Warning)
    return None

def default_band_list():
    return ["red","green","blue","nir","nir08","swir16","swir22","rededge1","rededge2","rededge3"]

def populate_band_combos(band1_combo, band2_combo, bands_meta=None):
    bands = list(bands_meta.keys()) if bands_meta else default_band_list()
    band1_combo.clear(); band2_combo.clear()
    band1_combo.addItems(bands)
    band2_combo.addItems([""] + bands)  

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
    except Exception:
        return 1

def qdate_to_iso(qdate):
    return qdate.toString("yyyy-MM-dd")
