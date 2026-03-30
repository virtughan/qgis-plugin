# virtughan_qgis/bootstrap.py
import importlib
import os
import shutil
import sys
from datetime import datetime

from qgis.core import Qgis, QgsApplication, QgsMessageLog
from qgis.PyQt.QtWidgets import QDialog, QPushButton, QTextEdit, QVBoxLayout, QMessageBox

from .dependency_versions import VIRTUGHAN_VERSION, runtime_package_specs

PKG_NAME = "virtughan"
DEFAULT_PACKAGES = runtime_package_specs()

_LAST_BOOTSTRAP_ERROR = None
PLUGIN_DIR = os.path.dirname(__file__)
VENDOR_DIR = os.path.join(PLUGIN_DIR, "vendor")
LIBS_DIR = os.path.join(PLUGIN_DIR, "libs")


def get_bootstrap_log_path() -> str:
    log_dir = os.path.join(QgsApplication.qgisSettingsDirPath(), "virtughan")
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, "bootstrap.log")


def _level_name(level) -> str:
    success_level = getattr(Qgis, "Success", None)
    if level == Qgis.Critical:
        return "CRITICAL"
    if level == Qgis.Warning:
        return "WARNING"
    if success_level is not None and level == success_level:
        return "SUCCESS"
    return "INFO"


def _append_file_log(msg: str, level=Qgis.Info):
    try:
        log_path = get_bootstrap_log_path()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{timestamp} [{_level_name(level)}] {msg}\n"
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        pass


def _log(msg: str, level=Qgis.Info):
    QgsMessageLog.logMessage(f"VirtuGhan Bootstrap: {msg}", "VirtuGhan", level)
    _append_file_log(msg, level)


def _set_last_error(msg: str | None):
    global _LAST_BOOTSTRAP_ERROR
    _LAST_BOOTSTRAP_ERROR = (msg or "").strip() or None


def get_last_bootstrap_error() -> str | None:
    return _LAST_BOOTSTRAP_ERROR


def _activate_vendor_paths() -> list[str]:
    candidate_dirs = [
        os.path.join(VENDOR_DIR, "site-packages"),
        VENDOR_DIR,
        os.path.join(LIBS_DIR, "site-packages"),
        LIBS_DIR,
    ]

    added: list[str] = []
    for path in candidate_dirs:
        if os.path.isdir(path) and path not in sys.path:
            sys.path.insert(0, path)
            added.append(path)

    if added:
        _log("Activated dependency paths: " + ", ".join(added))
    return added


def _parse_version_tuple(version_text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for raw in (version_text or "").strip().split("."):
        digits = ""
        for ch in raw:
            if ch.isdigit():
                digits += ch
            else:
                break
        if digits:
            parts.append(int(digits))
        else:
            parts.append(0)
    return tuple(parts)


def _is_installed_version_sufficient(installed: str, required: str) -> bool:
    return _parse_version_tuple(installed) >= _parse_version_tuple(required)


def _install_via_pip(packages: list[str]) -> bool:
    target = os.path.join(VENDOR_DIR, "site-packages")
    os.makedirs(target, exist_ok=True)

    try:
        from .pip_installer import install_dependencies

        success = install_dependencies(target, packages)
        if success:
            _log(f"Runtime installation successful: {', '.join(packages)}")
        else:
            _log("Runtime installation failed.", Qgis.Warning)
        return success
    except Exception as exc:
        _log(f"Runtime install exception: {exc}", Qgis.Critical)
        return False


def check_dependencies() -> bool:
    # Ensure we validate the currently installed files, not a stale in-memory module.
    for mod_name in list(sys.modules.keys()):
        if mod_name == "virtughan" or mod_name.startswith("virtughan."):
            try:
                del sys.modules[mod_name]
            except Exception:
                pass

    _activate_vendor_paths()
    importlib.invalidate_caches()
    try:
        import virtughan

        installed = getattr(virtughan, "__version__", "")
        if not _is_installed_version_sufficient(installed, VIRTUGHAN_VERSION):
            _log(
                f"VirtuGhan version too old ({installed or 'unknown'}). "
                f"Required: {VIRTUGHAN_VERSION}",
                Qgis.Warning,
            )
            return False

        _log(f"VirtuGhan package found (version {installed or 'unknown'})")
        return True
    except ImportError:
        _log("VirtuGhan package not found", Qgis.Warning)
        return False


def check_runtime_tls_bundle() -> tuple[bool, str | None]:
    """Validate TLS CA bundle availability (certifi) for HTTPS requests."""
    _activate_vendor_paths()
    importlib.invalidate_caches()
    try:
        import certifi  # noqa: F401

        bundle_path = certifi.where()
        if bundle_path and os.path.isfile(bundle_path):
            return True, None
        return False, f"Missing TLS CA bundle: {bundle_path}"
    except Exception as exc:
        return False, f"TLS certificate setup error: {exc}"


def ensure_runtime_network_ready(parent=None) -> bool:
    """
    Ensure runtime dependencies needed for network requests are healthy.

    Returns True when ready. If TLS bundle is missing, prompts user to repair.
    """
    ok, details = check_runtime_tls_bundle()
    if ok:
        return True

    _set_last_error(details)
    _log(details or "TLS CA bundle check failed", Qgis.Warning)

    if parent is None:
        return False

    reply = QMessageBox.question(
        parent,
        "VirtuGhan",
        "Runtime TLS certificates are missing or broken, so online data requests will fail.\n\n"
        "Do you want to repair dependencies now?",
        QMessageBox.Yes | QMessageBox.No,
        QMessageBox.Yes,
    )
    if reply != QMessageBox.Yes:
        return False

    repaired = repair_runtime_dependencies()
    if not repaired:
        warn = get_last_bootstrap_error() or "Could not fully clear dependency files."
        QMessageBox.warning(parent, "VirtuGhan", f"Repair completed with warnings:\n\n{warn}")

    installed = interactive_install_dependencies(parent)
    if not installed:
        err = get_last_bootstrap_error() or "Dependency reinstall failed."
        QMessageBox.critical(parent, "VirtuGhan", f"Dependency repair failed:\n\n{err}")
        return False

    ok_after, details_after = check_runtime_tls_bundle()
    if not ok_after:
        _set_last_error(details_after)
        QMessageBox.critical(
            parent,
            "VirtuGhan",
            f"Dependencies were reinstalled but TLS certificates are still unavailable:\n\n{details_after}",
        )
        return False

    return True


def install_dependencies(parent=None, quiet=False) -> bool:
    _set_last_error(None)

    if check_dependencies():
        return True

    _log("Attempting runtime dependency installation...", Qgis.Info)

    if _install_via_pip(DEFAULT_PACKAGES) and check_dependencies():
        return True

    _set_last_error(
        "Failed to install required dependencies at runtime.\n\n"
        "Please ensure internet is available on first run, or pre-bundle dependencies manually."
    )

    if not quiet and parent:
        _show_manual_install_dialog(parent)

    return False


def _show_manual_install_dialog(parent):
    try:
        log_path = get_bootstrap_log_path()
        dialog = QDialog(parent)
        dialog.setWindowTitle("Dependency Installation Failed")
        dialog.setMinimumSize(650, 420)

        layout = QVBoxLayout()

        instruction_text = (
            f"Could not auto-install '{PKG_NAME}'.\n\n"
            "Troubleshooting:\n"
            "1. Check internet connectivity\n"
            "2. Retry by restarting QGIS\n"
            "3. Check detailed log file:\n"
            f"   - {log_path}\n"
            "4. Or pre-vendor dependencies into: \n"
            f"   - {os.path.join(VENDOR_DIR, 'site-packages', PKG_NAME)}\n"
            f"   - {os.path.join(LIBS_DIR, PKG_NAME)}\n"
        )

        text_edit = QTextEdit()
        text_edit.setPlainText(instruction_text)
        text_edit.setReadOnly(True)
        layout.addWidget(text_edit)

        ok_button = QPushButton("OK")
        ok_button.clicked.connect(dialog.accept)
        layout.addWidget(ok_button)

        dialog.setLayout(layout)
        dialog.exec_()
    except Exception as exc:
        _log(f"Error showing manual install dialog: {exc}", Qgis.Warning)


def ensure_virtughan_installed(parent=None, quiet=True):
    try:
        return install_dependencies(parent, quiet)
    except Exception as exc:
        _log(f"Bootstrap error: {exc}", Qgis.Critical)
        _set_last_error(str(exc))
        return check_dependencies()


# ============================================================================
# Installation State Management (for first-time installer UI)
# ============================================================================


def get_install_state_path() -> str:
    """Get path to installation state flag file."""
    state_dir = os.path.join(QgsApplication.qgisSettingsDirPath(), "virtughan")
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "installed.flag")


def is_already_installed() -> bool:
    """Check if dependencies have been installed before."""
    flag_path = get_install_state_path()
    return os.path.isfile(flag_path)


def mark_as_installed():
    """Mark dependencies as installed by creating flag file."""
    try:
        flag_path = get_install_state_path()
        with open(flag_path, "w", encoding="utf-8") as f:
            f.write("installed\n")
        _log("Installation state saved")
    except Exception as exc:
        _log(f"Could not save installation state: {exc}", Qgis.Warning)


def clear_install_state():
    """Clear the installation state (for retries)."""
    try:
        flag_path = get_install_state_path()
        if os.path.isfile(flag_path):
            os.remove(flag_path)
        _log("Installation state cleared")
    except Exception as exc:
        _log(f"Could not clear installation state: {exc}", Qgis.Warning)


def _clear_runtime_site_packages() -> tuple[int, list[str]]:
    """Remove plugin-managed runtime packages under vendor/site-packages."""
    target = os.path.join(VENDOR_DIR, "site-packages")
    removed = 0
    failed: list[str] = []

    if not os.path.isdir(target):
        return removed, failed

    for name in os.listdir(target):
        path = os.path.join(target, name)
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            removed += 1
        except Exception as exc:
            failed.append(f"{name}: {exc}")

    return removed, failed


def repair_runtime_dependencies() -> bool:
    """Clear plugin-managed runtime dependencies and install state for a clean reinstall."""
    try:
        removed, failed = _clear_runtime_site_packages()

        clear_install_state()
        importlib.invalidate_caches()

        for mod_name in list(sys.modules.keys()):
            if mod_name.startswith("virtughan") or mod_name.startswith("rasterio"):
                try:
                    del sys.modules[mod_name]
                except Exception:
                    pass

        if failed:
            _set_last_error("Failed to remove some dependency files:\n" + "\n".join(failed[:20]))
            _log("Dependency repair completed with warnings", Qgis.Warning)
            return False

        _log(f"Dependency repair completed: removed {removed} entries", Qgis.Info)
        return True
    except Exception as exc:
        _set_last_error(str(exc))
        _log(f"Dependency repair failed: {exc}", Qgis.Critical)
        return False


def uninstall_runtime_dependencies() -> bool:
    """Uninstall plugin-managed runtime dependencies without reinstalling."""
    try:
        removed, failed = _clear_runtime_site_packages()
        clear_install_state()
        importlib.invalidate_caches()

        for mod_name in list(sys.modules.keys()):
            if mod_name.startswith("virtughan") or mod_name.startswith("rasterio"):
                try:
                    del sys.modules[mod_name]
                except Exception:
                    pass

        if failed:
            _set_last_error("Failed to remove some dependency files:\n" + "\n".join(failed[:20]))
            _log("Dependency uninstall completed with warnings", Qgis.Warning)
            return False

        _log(f"Dependency uninstall completed: removed {removed} entries", Qgis.Info)
        return True
    except Exception as exc:
        _set_last_error(str(exc))
        _log(f"Dependency uninstall failed: {exc}", Qgis.Critical)
        return False


def interactive_install_dependencies(parent=None) -> bool:
    """
    Show interactive installer dialog with real-time progress.
    
    Smart flow:
    1. Check if dependencies already import correctly
    2. If yes: Skip dialog, mark as installed, return True
    3. If no: Show dialog with real-time progress
    
    Returns True if installation succeeds, False otherwise.
    """
    _set_last_error(None)

    # Smart check: try to import first, skip dialog if everything works
    _activate_vendor_paths()
    if check_dependencies():
        _log("Dependencies already properly installed, skipping installer dialog")
        mark_as_installed()
        return True

    # Dependencies missing or broken - show interactive installer
    _log("Dependencies not found, showing installer dialog", Qgis.Info)

    # Define the installation callback
    def _do_install(progress_callback=None) -> bool:
        if progress_callback:
            progress_callback("Preparing installation...\n")

        if progress_callback:
            progress_callback("Checking for existing installation...\n")
        
        # Re-check in case it was installed just now
        _activate_vendor_paths()
        if check_dependencies():
            if progress_callback:
                progress_callback("Dependencies already available\n")
            return True

        if progress_callback:
            progress_callback("Starting package installation...\n")
        _log("Attempting interactive runtime dependency installation...", Qgis.Info)

        target = os.path.join(VENDOR_DIR, "site-packages")
        os.makedirs(target, exist_ok=True)

        try:
            from .pip_installer import install_dependencies as pip_install

            success = pip_install(target, DEFAULT_PACKAGES, progress_callback=progress_callback)

            if success:
                if progress_callback:
                    progress_callback("\nVerifying installation...\n")
                _activate_vendor_paths()
                importlib.invalidate_caches()
                if check_dependencies():
                    _log("Interactive runtime installation successful", Qgis.Info)
                    mark_as_installed()
                    return True

            _log("Interactive runtime installation failed", Qgis.Warning)
            _set_last_error(
                "Failed to install required dependencies at runtime.\n\n"
                "Please ensure internet is available."
            )
            return False

        except Exception as exc:
            _log(f"Interactive runtime install exception: {exc}", Qgis.Critical)
            if progress_callback:
                progress_callback(f"\nException: {exc}\n")
            return False

    # Show installer dialog
    try:
        from .installer_dialog import FirstTimeInstallerDialog

        dialog = FirstTimeInstallerDialog(parent, install_callback=_do_install)
        success = dialog.exec_with_install()

        if success:
            mark_as_installed()
            _log("First-time installation completed successfully", Qgis.Info)
        else:
            _log("First-time installation failed or cancelled", Qgis.Warning)

        return success

    except Exception as exc:
        _log(f"Error launching installer dialog: {exc}", Qgis.Critical)
        _set_last_error(f"Installer dialog failed: {exc}")
        # Fallback to silent install
        return install_dependencies(parent, quiet=False)
