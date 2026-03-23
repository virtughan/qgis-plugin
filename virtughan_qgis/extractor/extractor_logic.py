# virtughan_qgis/extractor/extractor_logic.py
import os, uuid, io, traceback, sys
from contextlib import redirect_stdout, redirect_stderr

from qgis.PyQt.QtCore import QDate, Qt
from qgis.core import (
    QgsProcessingAlgorithm, QgsProcessingParameterExtent,
    QgsProcessingParameterNumber, QgsProcessingParameterString, QgsProcessingParameterBoolean,
    QgsProcessingParameterFolderDestination, QgsProcessingUtils,
    QgsProcessingException, QgsProject, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
    QgsRasterLayer)

from ..common.common_logic import default_band_list

EXTRACTOR_IMPORT_ERROR = None
try:
    from virtughan.extract import ExtractProcessor
except Exception as e:
    ExtractProcessor = None
    EXTRACTOR_IMPORT_ERROR = e

VALID_BANDS = default_band_list()

def _coerce_to_qdate(val) -> QDate:
    if isinstance(val, QDate):
        return val
    s = "" if val is None else str(val).strip()
    if not s:
        return QDate()
    return QDate.fromString(s, Qt.ISODate)

def _extent_to_wgs84_bbox(extent, src_crs):
    wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    if not src_crs or not src_crs.isValid() or src_crs == wgs84:
        bbox = [extent.xMinimum(), extent.yMinimum(), extent.xMaximum(), extent.yMaximum()]
    else:
        xform = QgsCoordinateTransform(src_crs, wgs84, QgsProject.instance())
        ll = xform.transform(extent.xMinimum(), extent.yMinimum())
        ur = xform.transform(extent.xMaximum(), extent.yMaximum())
        bbox = [min(ll.x(), ur.x()), min(ll.y(), ur.y()), max(ll.x(), ur.x()), max(ll.y(), ur.y())]
    if (abs(bbox[0]) > 180 or abs(bbox[2]) > 180 or abs(bbox[1]) > 90 or abs(bbox[3]) > 90):
        raise QgsProcessingException(f"Converted bbox is not valid lon/lat: {bbox}")
    return bbox

class _FeedbackTee(io.TextIOBase):
    def __init__(self, file_obj, feedback):
        self.file = file_obj
        self.feedback = feedback
        self._buf = ""
    def write(self, s):
        if not s: return 0
        self.file.write(s); self.file.flush()
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                try: self.feedback.pushInfo(line)
                except Exception: pass
        return len(s)
    def flush(self):
        try: self.file.flush()
        except Exception: pass

class VirtuGhanExtractorAlgorithm(QgsProcessingAlgorithm):
    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterExtent("EXTENT", "Area of interest (any CRS)"))

        self.addParameter(QgsProcessingParameterString(
            "START_DATE", "Start date (YYYY-MM-DD)",
            defaultValue=QDate.currentDate().addMonths(-1).toString("yyyy-MM-dd")))
        self.addParameter(QgsProcessingParameterString(
            "END_DATE", "End date (YYYY-MM-DD)",
            defaultValue=QDate.currentDate().toString("yyyy-MM-dd")))

        self.addParameter(QgsProcessingParameterNumber(
            "CLOUD_COVER", "Max cloud cover (%)",
            type=QgsProcessingParameterNumber.Integer, defaultValue=30, minValue=0, maxValue=100))

        
        self.addParameter(QgsProcessingParameterString(
            "BANDS_LIST", "Bands to download (comma-separated from VALID_BANDS)",
            defaultValue="red,nir"))

        self.addParameter(QgsProcessingParameterBoolean(
            "ZIP_OUTPUT", "Zip output", defaultValue=False))
        self.addParameter(QgsProcessingParameterBoolean(
            "SMART_FILTER", "Apply smart filter", defaultValue=True))

        self.addParameter(QgsProcessingParameterNumber(
            "WORKERS", "Workers (0=auto)",
            type=QgsProcessingParameterNumber.Integer, defaultValue=1, minValue=0, maxValue=64))
        self.addParameter(QgsProcessingParameterFolderDestination(
            "OUTPUT_FOLDER", "Output folder (blank = temp)", optional=True))

    def name(self): return "virtughan_extractor"
    def displayName(self): return "VirtuGhan Extractor"
    def group(self): return "VirtuGhan"
    def groupId(self): return "virtughan"
    def shortHelpString(self): return "Extract Sentinel-2 bands from STAC API via ExtractProcessor."
    def createInstance(self): return VirtuGhanExtractorAlgorithm()

    def processAlgorithm(self, parameters, context, feedback):
        if ExtractProcessor is None:
            raise QgsProcessingException(f"ExtractProcessor import failed: {EXTRACTOR_IMPORT_ERROR}")

        
        extent = self.parameterAsExtent(parameters, "EXTENT", context)
        try:
            src_crs = self.parameterAsExtentCrs(parameters, "EXTENT", context)
        except Exception:
            src_crs = QgsProject.instance().crs()
        bbox = _extent_to_wgs84_bbox(extent, src_crs)
        feedback.pushInfo(f"AOI (EPSG:4326): {bbox}")

        
        sd_q = _coerce_to_qdate(self.parameterAsString(parameters, "START_DATE", context))
        ed_q = _coerce_to_qdate(self.parameterAsString(parameters, "END_DATE", context))
        if not sd_q.isValid() or not ed_q.isValid():
            raise QgsProcessingException("Invalid date. Use YYYY-MM-DD.")
        if sd_q > ed_q:
            raise QgsProcessingException("Start date must be before end date.")
        s = sd_q.toString("yyyy-MM-dd"); e = ed_q.toString("yyyy-MM-dd")

        
        cloud = max(0, min(100, int(self.parameterAsDouble(parameters, "CLOUD_COVER", context))))
        bands_csv = (self.parameterAsString(parameters, "BANDS_LIST", context) or "").strip()
        bands_list = [b.strip() for b in bands_csv.split(",") if b.strip()]
        for b in bands_list:
            if b not in VALID_BANDS:
                raise QgsProcessingException(f"Invalid band: {b}. Valid: {', '.join(VALID_BANDS)}")

        zip_out = self.parameterAsBool(parameters, "ZIP_OUTPUT", context)
        smart = self.parameterAsBool(parameters, "SMART_FILTER", context)

        workers = int(self.parameterAsDouble(parameters, "WORKERS", context))
        if workers <= 0:
            import multiprocessing
            workers = max(1, multiprocessing.cpu_count() - 1)

        out_base = (self.parameterAsString(parameters, "OUTPUT_FOLDER", context) or "").strip()
        if not out_base:
            out_base = getattr(context, "temporaryFolder", lambda: None)() or QgsProcessingUtils.tempFolder()
        out_dir = os.path.join(out_base, f"virtughan_extractor_{uuid.uuid4().hex[:8]}")
        os.makedirs(out_dir, exist_ok=True)
        log_path = os.path.join(out_dir, "runtime.log")

        feedback.pushInfo(f"Output: {out_dir}")
        feedback.pushInfo(f"Log file: {log_path}")

        with open(log_path, "a", encoding="utf-8", buffering=1) as lf:
            tee = _FeedbackTee(lf, feedback)
            with redirect_stdout(tee), redirect_stderr(tee):
                try:
                    print("Starting ExtractProcessor.extract()…", flush=True)
                    proc = ExtractProcessor(
                        bbox=bbox,
                        start_date=s,
                        end_date=e,
                        cloud_cover=cloud,
                        bands_list=bands_list,
                        output_dir=out_dir,
                        log_file=lf,
                        workers=workers,
                        zip_output=zip_out,
                        smart_filter=smart
                    )
                    proc.extract()
                    print("Extraction finished.", flush=True)
                except Exception:
                    print("[exception]", flush=True)
                    print(traceback.format_exc(), flush=True)
                    raise QgsProcessingException("ExtractProcessor.extract() failed – see runtime.log for details.")

        
        loaded = []
        for root, _dirs, files in os.walk(out_dir):
            for fn in files:
                if fn.lower().endswith((".tif", ".tiff", ".vrt")):
                    path = os.path.normpath(os.path.join(root, fn))
                    lyr = QgsRasterLayer(path, os.path.splitext(fn)[0], "gdal")
                    if lyr.isValid():
                        QgsProject.instance().addMapLayer(lyr, addToLegend=True)
                        loaded.append(path)
                        feedback.pushInfo(f"Loaded raster: {path}")
                    else:
                        feedback.reportError(f"Failed to load raster: {path}")

        return {"OUTPUT": out_dir, "RASTERS": loaded}
