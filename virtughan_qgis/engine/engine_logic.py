import os, uuid, io, traceback
from contextlib import redirect_stdout, redirect_stderr

from qgis.PyQt.QtCore import QDate, Qt
from qgis.core import (
    QgsProcessingAlgorithm, QgsProcessingParameterExtent,
    QgsProcessingParameterNumber, QgsProcessingParameterString, QgsProcessingParameterBoolean,
    QgsProcessingParameterEnum, QgsProcessingParameterFolderDestination, QgsProcessingUtils,
    QgsProcessingException, QgsProject, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
    QgsRasterLayer,
)

from ..bootstrap import activate_runtime_paths

activate_runtime_paths()

try:
    from qgis.core import QgsProcessingParameterDate
    HAVE_DATE_PARAM = True
except Exception:
    QgsProcessingParameterDate = None
    HAVE_DATE_PARAM = False

VIRTUGHAN_IMPORT_ERROR = None
try:
    from virtughan.engine import VirtughanProcessor
except Exception as e:
    VirtughanProcessor = None
    VIRTUGHAN_IMPORT_ERROR = e


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
        if not s:
            return 0
        self.file.write(s)
        self.file.flush()
        self._buf += s.replace("\r\n", "\n").replace("\r", "\n")
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                try:
                    self.feedback.pushInfo(line)
                except Exception:
                    pass
        return len(s)

    def flush(self):
        try:
            self.file.flush()
        except Exception:
            pass


class VirtuGhanEngineAlgorithm(QgsProcessingAlgorithm):
    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterExtent("EXTENT", "Area of interest (any CRS)"))

        if HAVE_DATE_PARAM:
            self.addParameter(QgsProcessingParameterString(
                "START_DATE", "Start date (YYYY-MM-DD)",
                defaultValue=QDate.currentDate().addMonths(-1).toString("yyyy-MM-dd")
            ))
            self.addParameter(QgsProcessingParameterString(
                "END_DATE", "End date (YYYY-MM-DD)",
                defaultValue=QDate.currentDate().toString("yyyy-MM-dd")
            ))
        else:
            self.addParameter(QgsProcessingParameterString(
                "START_DATE", "Start date (YYYY-MM-DD)",
                defaultValue=QDate.currentDate().addYears(-1).toString("yyyy-MM-dd")))
            self.addParameter(QgsProcessingParameterString(
                "END_DATE", "End date (YYYY-MM-DD)",
                defaultValue=QDate.currentDate().toString("yyyy-MM-dd")))

        self.addParameter(QgsProcessingParameterNumber(
            "CLOUD_COVER", "Max cloud cover (%)",
            type=QgsProcessingParameterNumber.Integer, defaultValue=30, minValue=0, maxValue=100))
        self.addParameter(QgsProcessingParameterString("FORMULA", "Formula",
            defaultValue="(band2-band1)/(band2+band1)"))
        self.addParameter(QgsProcessingParameterString("BAND1", "Band 1", defaultValue="red"))
        self.addParameter(QgsProcessingParameterString("BAND2", "Band 2 (optional)",
            defaultValue="nir", optional=True))
        self.addParameter(QgsProcessingParameterEnum(
            "OPERATION", "Aggregation",
            options=["mean","median","max","min","std","sum","var","none"], defaultValue=7))
        self.addParameter(QgsProcessingParameterBoolean(
            "TIMESERIES", "Generate timeseries", defaultValue=False))
        self.addParameter(QgsProcessingParameterBoolean(
            "SMART_FILTER", "Apply smart filter", defaultValue=False))
        self.addParameter(QgsProcessingParameterNumber(
            "WORKERS", "Workers (0=auto)",
            type=QgsProcessingParameterNumber.Integer, defaultValue=1, minValue=0, maxValue=64))
        self.addParameter(QgsProcessingParameterFolderDestination(
            "OUTPUT_FOLDER", "Output folder (blank = temp)", optional=True))

    def name(self): return "virtughan_engine"
    def displayName(self): return "VirtuGhan Compute"
    def group(self): return "VirtuGhan"
    def groupId(self): return "virtughan"
    def shortHelpString(self): return "Run VirtuGhan Compute from the Processing Toolbox."
    def createInstance(self): return VirtuGhanEngineAlgorithm()

    def processAlgorithm(self, parameters, context, feedback):
        if VIRTUGHAN_IMPORT_ERROR:
            raise QgsProcessingException(f"VirtughanProcessor import failed: {VIRTUGHAN_IMPORT_ERROR}")

        extent = self.parameterAsExtent(parameters, "EXTENT", context)
        try:
            src_crs = self.parameterAsExtentCrs(parameters, "EXTENT", context)
        except Exception:
            src_crs = QgsProject.instance().crs()
        bbox = _extent_to_wgs84_bbox(extent, src_crs)
        feedback.pushInfo(f"AOI (EPSG:4326): {bbox}")

        if HAVE_DATE_PARAM:
            sd = self.parameterAsDate(parameters, "START_DATE", context); sd = sd.date() if hasattr(sd, "date") else sd
            ed = self.parameterAsDate(parameters, "END_DATE", context);   ed = ed.date() if hasattr(ed, "date") else ed
            sd_q = sd if isinstance(sd, QDate) else _coerce_to_qdate(sd)
            ed_q = ed if isinstance(ed, QDate) else _coerce_to_qdate(ed)
        else:
            sd_q = _coerce_to_qdate(self.parameterAsString(parameters, "START_DATE", context))
            ed_q = _coerce_to_qdate(self.parameterAsString(parameters, "END_DATE", context))
        if not sd_q.isValid() or not ed_q.isValid():
            raise QgsProcessingException("Invalid date. Use YYYY-MM-DD.")
        if sd_q > ed_q:
            raise QgsProcessingException("Start date must be before end date.")
        s = sd_q.toString("yyyy-MM-dd"); e = ed_q.toString("yyyy-MM-dd")

        cloud = max(0, min(100, int(self.parameterAsDouble(parameters, "CLOUD_COVER", context))))
        formula = (self.parameterAsString(parameters, "FORMULA", context) or "").strip()
        band1 = (self.parameterAsString(parameters, "BAND1", context) or "").strip()
        band2 = (self.parameterAsString(parameters, "BAND2", context) or "").strip() or None
        if not formula: raise QgsProcessingException("Formula is required.")
        if not band1:   raise QgsProcessingException("Band 1 is required.")

        ops = ["mean","median","max","min","std","sum","var","none"]
        op_idx = self.parameterAsEnum(parameters, "OPERATION", context)
        op_txt = ops[op_idx] if 0 <= op_idx < len(ops) else "none"
        operation = None if op_txt == "none" else op_txt

        ts = self.parameterAsBool(parameters, "TIMESERIES", context)
        smart = self.parameterAsBool(parameters, "SMART_FILTER", context)

        if ts is False and operation is None:
            raise QgsProcessingException("Operation is required when 'Generate timeseries' is disabled.")

        workers = int(self.parameterAsDouble(parameters, "WORKERS", context))
        if workers <= 0:
            try:
                import multiprocessing
                workers = max(1, multiprocessing.cpu_count() - 1)
            except Exception:
                workers = 1

        out_base = (self.parameterAsString(parameters, "OUTPUT_FOLDER", context) or "").strip()
        if not out_base:
            out_base = getattr(context, "temporaryFolder", lambda: None)() or QgsProcessingUtils.tempFolder()
        out_dir = os.path.join(out_base, f"virtughan_engine_{uuid.uuid4().hex[:8]}")
        os.makedirs(out_dir, exist_ok=True)
        log_path = os.path.join(out_dir, "runtime.log")

        os.environ.setdefault("GDAL_HTTP_TIMEOUT", "30")
        os.environ.setdefault("CPL_DEBUG", "ON")
        os.environ["CPL_LOG"] = log_path

        feedback.pushInfo(f"Output: {out_dir}")
        feedback.pushInfo(f"Log file: {log_path}")
        feedback.pushInfo(f"Params: bbox={bbox}, start={s}, end={e}, cloud={cloud}, "
                          f"band1={band1}, band2={band2}, op={operation}, "
                          f"timeseries={ts}, workers={workers}, smart_filter={smart}")

        with open(log_path, "a", encoding="utf-8", buffering=1) as lf:
            tee = _FeedbackTee(lf, feedback)
            with redirect_stdout(tee), redirect_stderr(tee):
                try:
                    print("Starting compute() …", flush=True)
                    proc = VirtughanProcessor(
                        bbox=bbox,
                        start_date=s,
                        end_date=e,
                        cloud_cover=cloud,
                        formula=formula,
                        band1=band1,
                        band2=band2,
                        operation=operation,
                        timeseries=ts,
                        output_dir=out_dir,
                        log_file=lf,
                        cmap="RdYlGn",
                        workers=workers,
                        smart_filter=smart,
                    )
                    print("[checkpoint] entering VirtughanProcessor.compute()", flush=True)
                    proc.compute()
                    print("[checkpoint] exited VirtughanProcessor.compute()", flush=True)
                    print("compute() finished.", flush=True)
                except Exception:
                    print("[exception]", flush=True)
                    print(traceback.format_exc(), flush=True)
                    raise QgsProcessingException("VirtughanProcessor.compute() failed – see runtime.log for details.")

        loaded = []
        for root, _dirs, files in os.walk(out_dir):
            for fn in files:
                if fn.lower().endswith((".tif", ".tiff", ".vrt")):
                    path = os.path.normpath(os.path.join(root, fn))
                    name = os.path.splitext(fn)[0]
                    lyr = QgsRasterLayer(path, name, "gdal")
                    if lyr.isValid():
                        QgsProject.instance().addMapLayer(lyr, addToLegend=True)
                        loaded.append(path)
                        feedback.pushInfo(f"Loaded raster: {path}")
                    else:
                        feedback.reportError(f"Failed to load raster: {path}")

        if not loaded:
            feedback.pushInfo("No .tif/.tiff/.vrt files found in output folder to load.")
        else:
            feedback.pushInfo(f"Added {len(loaded)} raster(s) to the map.")

        return {"OUTPUT": out_dir, "RASTERS": loaded}
