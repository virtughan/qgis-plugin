# virtughan_qgis/main_plugin.py
import os
import sys

from qgis.PyQt.QtWidgets import QAction, QMessageBox
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.core import QgsApplication

from .common.map_setup import setup_default_map


PLUGIN_DIR = os.path.dirname(__file__)
LIBS_DIR = os.path.join(PLUGIN_DIR, "libs")
if os.path.isdir(LIBS_DIR) and LIBS_DIR not in sys.path:
    sys.path.insert(0, LIBS_DIR)

try:
    from .bootstrap import (
        get_last_bootstrap_error,
        interactive_install_dependencies,
        repair_runtime_dependencies,
    )
except Exception:
    def get_last_bootstrap_error():
        return None

    def interactive_install_dependencies(*args, **kwargs):
        return True

    def repair_runtime_dependencies(*args, **kwargs):
        return False


class VirtuGhanPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.engine_dock = None
        self.extractor_dock = None
        self.tiler_dock = None
        self.provider = None
        self.action_engine = None
        self.action_extractor = None
        self.action_tiler = None
        self.action_toolbar_open = None
        self.action_repair_dependencies = None
        self.toolbar = None
        self._hub_dialog = None
        self._results_history_session = []
        self._imports_ready = False
        self._last_import_error = None
        self._VirtughanHubDialog = None

    def _ensure_deps_and_imports(self):
        if self._imports_ready:
            return True

        ok = interactive_install_dependencies(self.iface.mainWindow())
        if not ok:
            details = get_last_bootstrap_error()
            self._last_import_error = "Dependency installation check failed."
            if details:
                self._last_import_error += f"\n\n{details}"
            return False

        try:
            from .engine.engine_widget import EngineDockWidget
            from .extractor.extractor_widget import ExtractorDockWidget
            from .tiler.tiler_widget import TilerDockWidget
            from .processing_provider import VirtuGhanProcessingProvider
            from .common.hub_dialog import VirtughanHubDialog
            self._EngineDockWidget = EngineDockWidget
            self._ExtractorDockWidget = ExtractorDockWidget
            self._TilerDockWidget = TilerDockWidget
            self._VirtuGhanProcessingProvider = VirtuGhanProcessingProvider
            self._VirtughanHubDialog = VirtughanHubDialog
            self._imports_ready = True
            return True
        except Exception as e:
            self._last_import_error = str(e)
            return False

    def initGui(self):
        if not self._ensure_deps_and_imports():
            QMessageBox.critical(
                self.iface.mainWindow(),
                "VirtuGhan",
                f"VirtuGhan plugin could not initialize:\n\n{self._last_import_error}"
            )
            self.action_engine = QAction("VirtuGhan • Engine (unavailable)", self.iface.mainWindow())
            self.action_engine.setEnabled(False)
            self.action_extractor = QAction("VirtuGhan • Extractor (unavailable)", self.iface.mainWindow())
            self.action_extractor.setEnabled(False)
            self.iface.addPluginToMenu("VirtuGhan", self.action_engine)
            self.iface.addPluginToMenu("VirtuGhan", self.action_extractor)
            return

        self.action_engine = QAction("VirtuGhan • Engine", self.iface.mainWindow())
        self.action_engine.triggered.connect(self.show_engine)
        self.iface.addPluginToMenu("VirtuGhan", self.action_engine)

        self.action_extractor = QAction("VirtuGhan • Extractor", self.iface.mainWindow())
        self.action_extractor.triggered.connect(self.show_extractor)
        self.iface.addPluginToMenu("VirtuGhan", self.action_extractor)

        self.action_tiler = QAction("VirtuGhan • Tiler", self.iface.mainWindow())
        self.action_tiler.triggered.connect(self.show_tiler)
        self.iface.addPluginToMenu("VirtuGhan", self.action_tiler)

        self.action_repair_dependencies = QAction("VirtuGhan • Repair Dependencies", self.iface.mainWindow())
        self.action_repair_dependencies.triggered.connect(self.repair_dependencies)
        self.iface.addPluginToMenu("VirtuGhan", self.action_repair_dependencies)

        self.toolbar = self.iface.addToolBar("VirtuGhan")
        self.toolbar.setObjectName("VirtuGhanToolbar")
        logo_path = os.path.join(PLUGIN_DIR, "static", "images", "virtughan-logo.png")
        self.action_toolbar_open = QAction(QIcon(logo_path), "VirtuGhan", self.iface.mainWindow())
        self.action_toolbar_open.setToolTip("Open VirtuGhan")
        self.action_toolbar_open.triggered.connect(self.show_engine)
        self.toolbar.addAction(self.action_toolbar_open)

        try:
            self.provider = self._VirtuGhanProcessingProvider()
            QgsApplication.processingRegistry().addProvider(self.provider)
        except Exception as e:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "VirtuGhan",
                f"Processing provider could not be registered:\n{e}"
            )

    def unload(self):
        try:
            if self._hub_dialog:
                self._hub_dialog.close()
        except Exception:
            pass
        self._hub_dialog = None

        if self.action_engine:
            self.iface.removePluginMenu("VirtuGhan", self.action_engine)
            self.action_engine = None
        if self.engine_dock:
            self.iface.removeDockWidget(self.engine_dock)
            self.engine_dock = None

        if self.action_extractor:
            self.iface.removePluginMenu("VirtuGhan", self.action_extractor)
            self.action_extractor = None
        if self.extractor_dock:
            self.iface.removeDockWidget(self.extractor_dock)
            self.extractor_dock = None

        if self.action_tiler:
            self.iface.removePluginMenu("VirtuGhan", self.action_tiler)
            self.action_tiler = None
        if self.tiler_dock:
            self.iface.removeDockWidget(self.tiler_dock)
            self.tiler_dock = None

        if self.provider:
            try:
                QgsApplication.processingRegistry().removeProvider(self.provider)
            except Exception:
                pass
            self.provider = None 

        if self.action_toolbar_open:
            self.action_toolbar_open = None

        if self.action_repair_dependencies:
            self.iface.removePluginMenu("VirtuGhan", self.action_repair_dependencies)
            self.action_repair_dependencies = None

        if self.toolbar:
            try:
                self.iface.mainWindow().removeToolBar(self.toolbar)
            except Exception:
                pass
            self.toolbar = None

    def _show_hub(self, start_page: str):
        # Optional: add basemap once per click, but skip if already present
        try:
            if getattr(self.iface, "mapCanvas", None) and self.iface.mapCanvas():
                setup_default_map(
                    self.iface,
                    center_wgs84=(85.3478258, 27.6934185),
                    scale_m=5000,
                    set_project_crs=False,            # respect current project CRS
                    skip_if_present=True,             # don't add another OSM if present
                    skip_zoom_if_present=True,        # don't recenter if OSM already present
                    zoom_delay_ms=1000,
                )
        except Exception as e:
            # skip if osm map already exists and any issues with map loading
            try:
                self.iface.messageBar().pushWarning("VirtuGhan", f"Basemap skipped: {e}")
            except Exception:
                pass

        # Reuse existing dialog instance if it is still alive (even if hidden/minimized)
        if self._hub_dialog is not None:
            try:
                if hasattr(self._hub_dialog, "show_page"):
                    self._hub_dialog.show_page(start_page)
                self._hub_dialog.show()
                self._hub_dialog.raise_()
                self._hub_dialog.activateWindow()
                return
            except RuntimeError:
                self._hub_dialog = None
            except Exception:
                pass

        # Close previous instance if you want only one hub at a time
        try:
            if self._hub_dialog:
                try:
                    self._results_history_session = self._hub_dialog.get_results_history_snapshot()
                except Exception:
                    pass
                self._hub_dialog.close()
        except Exception:
            pass

        self._hub_dialog = self._VirtughanHubDialog(self.iface, start_page=start_page, parent=self.iface.mainWindow())
        try:
            if self._results_history_session:
                self._hub_dialog.set_results_history(self._results_history_session)
        except Exception:
            pass
        self._hub_dialog.finished.connect(self._on_hub_finished)
        self._hub_dialog.setModal(False)
        self._hub_dialog.setAttribute(Qt.WA_DeleteOnClose, True)
        self._hub_dialog.show()
        self._hub_dialog.raise_()

    def _on_hub_finished(self, _result: int):
        try:
            if self._hub_dialog:
                self._results_history_session = self._hub_dialog.get_results_history_snapshot()
        except Exception:
            pass
        self._hub_dialog = None


    def show_engine(self):
        self._show_hub("engine")

    def show_extractor(self):
        self._show_hub("extractor")

    def show_tiler(self):
        self._show_hub("tiler")

    def repair_dependencies(self):
        reply = QMessageBox.question(
            self.iface.mainWindow(),
            "VirtuGhan",
            "This will clear plugin runtime dependencies and reinstall them.\n\nContinue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        repaired = repair_runtime_dependencies()
        if not repaired:
            details = get_last_bootstrap_error() or "Some files could not be cleared."
            QMessageBox.warning(
                self.iface.mainWindow(),
                "VirtuGhan",
                f"Dependency repair completed with warnings:\n\n{details}",
            )

        ok = interactive_install_dependencies(self.iface.mainWindow())
        if ok:
            QMessageBox.information(
                self.iface.mainWindow(),
                "VirtuGhan",
                "Dependencies repaired and reinstalled successfully.\n\nPlease restart QGIS for a clean runtime reload.",
            )
        else:
            details = get_last_bootstrap_error() or "Dependency installation failed."
            QMessageBox.critical(
                self.iface.mainWindow(),
                "VirtuGhan",
                f"Dependency reinstall failed:\n\n{details}",
            )



