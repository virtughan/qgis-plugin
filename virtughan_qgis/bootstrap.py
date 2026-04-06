# virtughan_qgis/bootstrap.py
import importlib
import os
import shutil
import sys
from datetime import datetime
from importlib import metadata as importlib_metadata

from qgis.core import Qgis, QgsApplication, QgsMessageLog
from qgis.PyQt.QtWidgets import QDialog, QPushButton, QTextEdit, QVBoxLayout, QMessageBox
from qgis.PyQt.QtCore import QSettings

from .dependency_versions import (
    VIRTUGHAN_VERSION,
    RASTERIO_VERSION,
    runtime_package_specs,
)

PKG_NAME = "virtughan"
DEFAULT_PACKAGES = runtime_package_specs()

_LAST_BOOTSTRAP_ERROR = None
PLUGIN_DIR = os.path.dirname(__file__)
RUNTIME_ROOT = os.path.join(QgsApplication.qgisSettingsDirPath(), "virtughan_runtime")
RUNTIME_SITE_PACKAGES_DIR = os.path.join(RUNTIME_ROOT, "site-packages")
RUNTIME_FALLBACK_ROOT = os.path.join(QgsApplication.qgisSettingsDirPath(), "virtughan_runtime_fallback")
RUNTIME_FALLBACK_SITE_PACKAGES_DIR = os.path.join(RUNTIME_FALLBACK_ROOT, "site-packages")
UNINSTALL_ON_PLUGIN_UNINSTALL_KEY = "virtughan/dependencies/uninstall_on_plugin_uninstall"


def get_uninstall_on_plugin_uninstall() -> bool:
    try:
        return bool(QSettings().value(UNINSTALL_ON_PLUGIN_UNINSTALL_KEY, True, type=bool))
    except Exception:
        return True


def set_uninstall_on_plugin_uninstall(enabled: bool):
    try:
        QSettings().setValue(UNINSTALL_ON_PLUGIN_UNINSTALL_KEY, bool(enabled))
    except Exception:
        pass


def _runtime_site_packages_candidates() -> list[str]:
    # Primary runtime first; fallback is used only if primary fails/locks.
    return [RUNTIME_SITE_PACKAGES_DIR, RUNTIME_FALLBACK_SITE_PACKAGES_DIR]


def get_bootstrap_log_path() -> str:
    log_dir = os.path.join(QgsApplication.qgisSettingsDirPath(), "virtughan")
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, "bootstrap.log")


def get_runtime_restart_guard_path() -> str:
    guard_dir = os.path.join(QgsApplication.qgisSettingsDirPath(), "virtughan")
    os.makedirs(guard_dir, exist_ok=True)
    return os.path.join(guard_dir, "restart_required.flag")


def mark_runtime_restart_required(reason: str = "runtime dependencies updated"):
    try:
        guard_path = get_runtime_restart_guard_path()
        with open(guard_path, "w", encoding="utf-8") as fh:
            fh.write(f"pid={os.getpid()}\n")
            fh.write(f"reason={reason}\n")
        _log(f"Marked runtime restart required ({reason})", Qgis.Info)
    except Exception as exc:
        _log(f"Could not mark runtime restart requirement: {exc}", Qgis.Warning)


def clear_runtime_restart_required():
    try:
        guard_path = get_runtime_restart_guard_path()
        if os.path.isfile(guard_path):
            os.remove(guard_path)
    except Exception as exc:
        _log(f"Could not clear runtime restart requirement: {exc}", Qgis.Warning)


def is_runtime_restart_required() -> bool:
    guard_path = get_runtime_restart_guard_path()
    if not os.path.isfile(guard_path):
        return False

    try:
        with open(guard_path, "r", encoding="utf-8") as fh:
            lines = [line.strip() for line in fh.readlines() if line.strip()]
    except Exception:
        return False

    pid_line = next((line for line in lines if line.startswith("pid=")), "")
    if not pid_line:
        return False

    try:
        marked_pid = int(pid_line.split("=", 1)[1])
    except Exception:
        return False

    if marked_pid != os.getpid():
        # New QGIS session detected; clear stale marker and allow execution.
        clear_runtime_restart_required()
        return False

    return True


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


def _activate_vendor_paths(preferred_site_packages: str | None = None) -> list[str]:
    default_candidates = [
        RUNTIME_SITE_PACKAGES_DIR,
        RUNTIME_ROOT,
        RUNTIME_FALLBACK_SITE_PACKAGES_DIR,
        RUNTIME_FALLBACK_ROOT,
    ]

    candidate_dirs: list[str] = []
    if preferred_site_packages:
        preferred_root = os.path.dirname(preferred_site_packages)
        candidate_dirs.extend([preferred_site_packages, preferred_root])
    for path in default_candidates:
        if path not in candidate_dirs:
            candidate_dirs.append(path)

    activated: list[str] = []
    for path in candidate_dirs:
        if not os.path.isdir(path):
            continue

        # Always prioritize runtime paths, even when already present in sys.path.
        while path in sys.path:
            try:
                sys.path.remove(path)
            except Exception:
                break
        sys.path.insert(0, path)
        activated.append(path)

    if activated:
        _log("Activated dependency paths: " + ", ".join(activated))
    return activated


def activate_runtime_paths() -> list[str]:
    return _activate_vendor_paths()


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


def _get_installed_virtughan_version(module) -> str:
    version = (getattr(module, "__version__", "") or "").strip()
    if version:
        return version

    try:
        return (importlib_metadata.version("virtughan") or "").strip()
    except Exception:
        return ""


def _get_installed_distribution_version(dist_name: str, module=None) -> str:
    version = (getattr(module, "__version__", "") or "").strip() if module else ""
    if version:
        return version

    try:
        return (importlib_metadata.version(dist_name) or "").strip()
    except Exception:
        return ""


def _is_installed_version_exact(installed: str, required: str) -> bool:
    if not installed or not required:
        return False
    return _parse_version_tuple(installed) == _parse_version_tuple(required)


def _is_module_loaded_from_runtime(module) -> bool:
    mod_file = getattr(module, "__file__", "") or ""
    return _is_path_loaded_from_runtime(mod_file)


def _is_path_loaded_from_runtime(path: str) -> bool:
    mod_file = os.path.normcase(os.path.normpath(path or ""))
    if not mod_file:
        return False
    runtime_sites = [
        os.path.normcase(os.path.normpath(candidate))
        for candidate in _runtime_site_packages_candidates()
    ]
    for runtime_site in runtime_sites:
        try:
            common = os.path.commonpath([mod_file, runtime_site])
        except Exception:
            continue
        if common == runtime_site:
            return True
    return False


def _is_runtime_lock_failure(message: str) -> bool:
    text = (message or "").lower()
    markers = [
        "winerror 5",
        "access is denied",
        "permissionerror",
        "being used by another process",
        "still exists after delete attempt",
    ]
    return any(marker in text for marker in markers)


def _infer_numpy_compat_spec(error_text: str) -> str | None:
    text = (error_text or "").lower()
    if not text:
        return None

    # NumPy ABI guardrails from compiled extension import errors.
    if (
        "compiled using numpy 1.x cannot be run in numpy 2" in text
        or ("compiled using numpy 1.x" in text and "cannot be run in numpy 2" in text)
        or "downgrade to 'numpy<2'" in text
    ):
        return "numpy<2"

    if (
        ("compiled using numpy 2" in text and "cannot be run in numpy 1" in text)
        or "upgrade to 'numpy>=2'" in text
    ):
        return "numpy>=2"

    # Conservative fallback: when rasterio import specifically fails due to numpy, prefer NumPy 1.x ABI.
    if "rasterio import failed" in text and "numpy" in text:
        return "numpy<2"

    return None


def _clear_runtime_module_cache():
    for mod_name in list(sys.modules.keys()):
        if (
            mod_name.startswith("virtughan")
            or mod_name.startswith("rasterio")
            or mod_name.startswith("numpy")
        ):
            try:
                del sys.modules[mod_name]
            except Exception:
                pass


def _clear_pip_cache(progress_callback=None) -> tuple[int, list[str]]:
    """Best-effort cleanup of pip caches to force fresh wheel/index resolution."""
    candidates: list[str] = []

    pip_cache_env = os.environ.get("PIP_CACHE_DIR", "").strip()
    if pip_cache_env:
        candidates.append(pip_cache_env)

    xdg_cache_home = os.environ.get("XDG_CACHE_HOME", "").strip()
    if xdg_cache_home:
        candidates.append(os.path.join(xdg_cache_home, "pip"))

    candidates.extend(
        [
            os.path.join(os.path.expanduser("~"), ".cache", "pip"),
            os.path.join(os.path.expanduser("~"), "Library", "Caches", "pip"),
            os.path.join(os.path.expanduser("~"), "AppData", "Local", "pip", "Cache"),
        ]
    )

    unique_candidates: list[str] = []
    seen: set[str] = set()
    for path in candidates:
        norm = os.path.normcase(os.path.normpath(path))
        if norm in seen:
            continue
        seen.add(norm)
        unique_candidates.append(path)

    def _on_rm_error(func, path, _exc_info):
        try:
            os.chmod(path, 0o700)
            func(path)
        except Exception:
            pass

    removed = 0
    failed: list[str] = []
    for cache_dir in unique_candidates:
        if not os.path.isdir(cache_dir):
            continue
        try:
            shutil.rmtree(cache_dir, onerror=_on_rm_error)
            if os.path.isdir(cache_dir):
                failed.append(f"{cache_dir}: cache folder still exists after delete attempt")
            else:
                removed += 1
        except Exception as exc:
            failed.append(f"{cache_dir}: {exc}")

    if removed:
        _log(f"Pip cache cleared: removed {removed} cache folder(s)", Qgis.Info)
        if progress_callback:
            progress_callback(f"Cleared pip cache folders: {removed}")
    elif progress_callback:
        progress_callback("No pip cache folders found to clear.")

    if failed:
        _log("Pip cache cleanup completed with warnings", Qgis.Warning)
        if progress_callback:
            progress_callback("Pip cache cleanup warnings: " + " | ".join(failed[:5]))

    return removed, failed


def _preflight_runtime_reinstall(progress_callback=None) -> tuple[bool, list[str]]:
    """Prepare clean install targets before pip install starts.

    Returns:
        (True, targets) when at least one runtime target can be used.
        (False, []) when all targets are blocked (typically by file locks).
    """
    removed, failed = _clear_runtime_site_packages()
    clear_install_state()
    importlib.invalidate_caches()
    _clear_runtime_module_cache()

    runtime_pairs = [
        (RUNTIME_ROOT, RUNTIME_SITE_PACKAGES_DIR),
        (RUNTIME_FALLBACK_ROOT, RUNTIME_FALLBACK_SITE_PACKAGES_DIR),
    ]

    blocked_roots: set[str] = set()
    for item in failed:
        for root, _site in runtime_pairs:
            if item.startswith(f"{root}:"):
                blocked_roots.add(root)

    targets: list[str] = []
    for root, site in runtime_pairs:
        if root in blocked_roots:
            continue
        targets.append(site)

    if failed:
        details = "Failed to remove some dependency files:\n" + "\n".join(failed[:20])
        _log(details, Qgis.Warning)
        if progress_callback:
            progress_callback(f"Cleanup warnings: {details}")

    if not targets:
        lock_detected = any(_is_runtime_lock_failure(item) for item in failed)
        if lock_detected:
            msg = (
                "Runtime folders are locked by this QGIS session and could not be cleaned. "
                "Please restart QGIS and try installation again."
            )
        else:
            msg = (
                "Could not prepare any runtime install folder. "
                "Please restart QGIS and try again."
            )
        _set_last_error(msg)
        _log(msg, Qgis.Warning)
        if progress_callback:
            progress_callback(msg)
        return False, []

    _log(
        f"Runtime preflight ready: removed={removed}, targets={', '.join(targets)}",
        Qgis.Info,
    )
    return True, targets


def _install_via_pip(packages: list[str], progress_callback=None, targets: list[str] | None = None) -> bool:
    try:
        from .pip_installer import get_last_install_error, install_dependencies

        install_targets = targets or _runtime_site_packages_candidates()
        saw_lock_issue = False
        for idx, target in enumerate(install_targets):
            os.makedirs(target, exist_ok=True)
            if progress_callback:
                progress_callback(f"Installing runtime packages into: {target}")

            success = install_dependencies(target, packages, progress_callback=progress_callback)

            # Some Windows sessions can report pip failure after writing files due to
            # locked native modules during target cleanup. Always verify runtime imports.
            _activate_vendor_paths(preferred_site_packages=target)
            importlib.invalidate_caches()
            deps_ok = check_dependencies(preferred_site_packages=target)

            if success and deps_ok:
                _log(f"Runtime installation successful: {', '.join(packages)}")
                return True

            if not success and deps_ok:
                _log(
                    "Runtime installation completed with pip warnings, but dependencies verified successfully.",
                    Qgis.Warning,
                )
                return True

            pip_err = get_last_install_error() or ""
            if "locked by the current QGIS session" in pip_err:
                saw_lock_issue = True
                _log(pip_err, Qgis.Warning)
                if progress_callback:
                    progress_callback(pip_err)

            if not deps_ok:
                abi_hint = _infer_numpy_compat_spec(
                    f"{pip_err}\n{get_last_bootstrap_error() or ''}"
                )
                if abi_hint:
                    msg = (
                        f"Detected NumPy ABI mismatch; retrying install with constraint '{abi_hint}' "
                        f"in {target}."
                    )
                    _log(msg, Qgis.Warning)
                    if progress_callback:
                        progress_callback(msg)

                    retry_packages = [
                        pkg for pkg in packages if not pkg.strip().lower().startswith("numpy")
                    ]
                    retry_packages.append(abi_hint)

                    retry_success = install_dependencies(
                        target,
                        retry_packages,
                        progress_callback=progress_callback,
                    )
                    _activate_vendor_paths(preferred_site_packages=target)
                    importlib.invalidate_caches()
                    retry_deps_ok = check_dependencies(preferred_site_packages=target)

                    if retry_success and retry_deps_ok:
                        _log(
                            "Runtime installation successful after NumPy ABI compatibility retry."
                        )
                        return True

                    if not retry_success and retry_deps_ok:
                        _log(
                            "Runtime installation completed with pip warnings after NumPy ABI retry, "
                            "but dependencies verified successfully.",
                            Qgis.Warning,
                        )
                        return True

            if idx < len(install_targets) - 1:
                msg = (
                    f"Runtime installation did not verify cleanly in {target}; "
                    "retrying in fallback runtime folder."
                )
                _log(msg, Qgis.Warning)
                if progress_callback:
                    progress_callback(msg)

        if saw_lock_issue:
            restart_msg = (
                "Runtime installation failed because dependency files are locked by this QGIS session. "
                "Please restart QGIS and try installation again."
            )
        else:
            restart_msg = (
                "Runtime installation failed in both primary and fallback folders. "
                "Please restart QGIS and try installation again."
            )
        _set_last_error(restart_msg)
        _log(restart_msg, Qgis.Warning)
        if progress_callback:
            progress_callback(restart_msg)
        return False
    except Exception as exc:
        _log(f"Runtime install exception: {exc}", Qgis.Critical)
        return False


def check_dependencies(preferred_site_packages: str | None = None) -> bool:
    # Ensure we validate the currently installed files, not a stale in-memory module.
    for mod_name in list(sys.modules.keys()):
        if (
            mod_name == "virtughan"
            or mod_name.startswith("virtughan.")
            or mod_name == "rasterio"
            or mod_name.startswith("rasterio.")
            or mod_name == "numpy"
            or mod_name.startswith("numpy.")
        ):
            try:
                del sys.modules[mod_name]
            except Exception:
                pass

    _activate_vendor_paths(preferred_site_packages)
    importlib.invalidate_caches()
    try:
        virtughan_spec = importlib.util.find_spec("virtughan")
        if virtughan_spec is None:
            _set_last_error("VirtuGhan package not found")
            _log("VirtuGhan package not found", Qgis.Warning)
            return False
    except Exception as exc:
        _set_last_error(f"VirtuGhan discovery failed: {exc}")
        _log(f"VirtuGhan discovery failed: {exc}", Qgis.Warning)
        return False

    installed = _get_installed_distribution_version("virtughan")
    virtughan_origin = getattr(virtughan_spec, "origin", "") or ""
    if not _is_path_loaded_from_runtime(virtughan_origin):
        details = (
            "VirtuGhan was discovered from a non-runtime path: "
            f"{virtughan_origin}. Expected under: {RUNTIME_SITE_PACKAGES_DIR}"
        )
        _set_last_error(details)
        _log(details, Qgis.Warning)
        return False

    if installed and not _is_installed_version_sufficient(installed, VIRTUGHAN_VERSION):
        details = (
            f"VirtuGhan version too old ({installed}). Required: {VIRTUGHAN_VERSION}"
        )
        _set_last_error(details)
        _log(details, Qgis.Warning)
        return False

    if not _is_installed_version_exact(installed, VIRTUGHAN_VERSION):
        details = f"VirtuGhan version mismatch ({installed}). Expected: {VIRTUGHAN_VERSION}"
        _set_last_error(details)
        _log(details, Qgis.Warning)
        return False

    try:
        rasterio_spec = importlib.util.find_spec("rasterio")
        if rasterio_spec is None:
            _set_last_error("rasterio package not found")
            _log("rasterio package not found", Qgis.Warning)
            return False
    except Exception as exc:
        _set_last_error(f"rasterio discovery failed: {exc}")
        _log(f"rasterio discovery failed: {exc}", Qgis.Warning)
        return False

    rasterio_origin = getattr(rasterio_spec, "origin", "") or ""
    if not _is_path_loaded_from_runtime(rasterio_origin):
        details = (
            "rasterio was discovered from a non-runtime path: "
            f"{rasterio_origin}. Expected under: {RUNTIME_SITE_PACKAGES_DIR}"
        )
        _set_last_error(details)
        _log(details, Qgis.Warning)
        return False

    rasterio_version = _get_installed_distribution_version("rasterio", None)
    if not _is_installed_version_exact(rasterio_version, RASTERIO_VERSION):
        details = f"rasterio version mismatch ({rasterio_version}). Expected: {RASTERIO_VERSION}"
        _set_last_error(details)
        _log(details, Qgis.Warning)
        return False

    try:
        numpy_spec = importlib.util.find_spec("numpy")
        if numpy_spec is None:
            _set_last_error("numpy package not found")
            _log("numpy package not found", Qgis.Warning)
            return False
    except Exception as exc:
        _set_last_error(f"numpy discovery failed: {exc}")
        _log(f"numpy discovery failed: {exc}", Qgis.Warning)
        return False

    numpy_origin = getattr(numpy_spec, "origin", "") or ""
    if not _is_path_loaded_from_runtime(numpy_origin):
        details = (
            "numpy was discovered from a non-runtime path: "
            f"{numpy_origin}. Expected under: {RUNTIME_SITE_PACKAGES_DIR}"
        )
        _set_last_error(details)
        _log(details, Qgis.Warning)
        return False

    numpy_version = _get_installed_distribution_version("numpy", None)
    if not numpy_version:
        details = "numpy version could not be determined"
        _set_last_error(details)
        _log(details, Qgis.Warning)
        return False

    _set_last_error(None)
    _log(
        "Runtime dependencies verified: "
        f"virtughan={installed}, rasterio={rasterio_version}, numpy={numpy_version}"
    )
    return True


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


def _check_plugin_import_health() -> tuple[bool, str | None]:
    """Verify non-Tiler plugin modules are discoverable without importing them."""
    module_names = [
        "virtughan_qgis.engine.engine_widget",
        "virtughan_qgis.extractor.extractor_widget",
        "virtughan_qgis.processing_provider",
        "virtughan_qgis.common.hub_dialog",
    ]
    for name in module_names:
        try:
            if importlib.util.find_spec(name) is None:
                return False, f"Plugin module not found: {name}"
        except Exception as exc:
            return False, f"Plugin module discovery failed ({name}): {exc}"
    return True, None


def ensure_runtime_network_ready(parent=None) -> bool:
    """
    Ensure runtime dependencies needed for network requests are healthy.

    Returns True when ready. If TLS bundle is missing, prompts user to repair.
    """
    restart_flag_set = is_runtime_restart_required()
    if restart_flag_set:
        _log(
            "Runtime dependencies were updated in this QGIS session; "
            "validating refreshed runtime without restart.",
            Qgis.Info,
        )

    ok, details = check_runtime_tls_bundle()
    if ok:
        if restart_flag_set:
            clear_runtime_restart_required()
            _log("Runtime refresh validated; restart requirement cleared.", Qgis.Info)
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

    clear_runtime_restart_required()

    return True


def install_dependencies(parent=None, quiet=False) -> bool:
    _set_last_error(None)

    if check_dependencies():
        return True

    # If deps are unhealthy, prepare clean runtime targets before reinstall.
    preflight_ok, install_targets = _preflight_runtime_reinstall()
    if not preflight_ok:
        if not quiet and parent:
            _show_manual_install_dialog(parent)
        return False

    _log("Attempting runtime dependency installation...", Qgis.Info)

    if _install_via_pip(DEFAULT_PACKAGES, targets=install_targets) and check_dependencies():
        mark_runtime_restart_required("runtime dependencies installed")
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
            "4. Or pre-vendor dependencies into:\n"
            f"   - {os.path.join(RUNTIME_SITE_PACKAGES_DIR, PKG_NAME)}\n"
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
    """Remove plugin-managed runtime dependencies from profile runtime root."""
    runtime_roots = [RUNTIME_ROOT, RUNTIME_FALLBACK_ROOT]
    if not any(os.path.isdir(root) for root in runtime_roots):
        return 0, []

    def _on_rm_error(func, path, _exc_info):
        try:
            os.chmod(path, 0o700)
            func(path)
        except Exception:
            pass

    removed = 0
    failed: list[str] = []
    for root in runtime_roots:
        if not os.path.isdir(root):
            continue
        try:
            # Prefer removing whole runtime root so uninstalls are complete.
            shutil.rmtree(root, onerror=_on_rm_error)
            if os.path.isdir(root):
                failed.append(f"{root}: runtime folder still exists after delete attempt")
            else:
                removed += 1
        except Exception as exc:
            failed.append(f"{root}: {exc}")

    return removed, failed


def repair_runtime_dependencies(clear_pip_cache: bool = False, progress_callback=None) -> bool:
    """Clear plugin-managed runtime dependencies and install state for a clean reinstall."""
    try:
        if clear_pip_cache:
            _clear_pip_cache(progress_callback=progress_callback)

        removed, failed = _clear_runtime_site_packages()

        clear_install_state()
        importlib.invalidate_caches()

        for mod_name in list(sys.modules.keys()):
            if (
                mod_name.startswith("virtughan")
                or mod_name.startswith("rasterio")
                or mod_name.startswith("numpy")
            ):
                try:
                    del sys.modules[mod_name]
                except Exception:
                    pass

        if failed:
            lock_failures = [item for item in failed if _is_runtime_lock_failure(item)]
            if lock_failures and len(lock_failures) == len(failed):
                _set_last_error(
                    "Runtime dependency folders are locked by this QGIS session. "
                    "If one folder is locked, reinstall will try the alternate runtime folder automatically. "
                    "If both folders are locked, restart QGIS and try again."
                )
            else:
                _set_last_error("Failed to remove some dependency files:\n" + "\n".join(failed[:20]))
            _log("Dependency repair completed with warnings", Qgis.Warning)
            return False

        _log(f"Dependency repair completed: removed {removed} entries", Qgis.Info)
        if clear_pip_cache:
            _log("Dependency repair used fresh mode (pip cache cleared)", Qgis.Info)
        mark_runtime_restart_required("runtime dependencies repaired")
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
            if (
                mod_name.startswith("virtughan")
                or mod_name.startswith("rasterio")
                or mod_name.startswith("numpy")
            ):
                try:
                    del sys.modules[mod_name]
                except Exception:
                    pass

        if failed:
            lock_failures = [item for item in failed if _is_runtime_lock_failure(item)]
            if lock_failures and len(lock_failures) == len(failed):
                _set_last_error(
                    "Runtime dependency folders are locked by this QGIS session. "
                    "Restart QGIS to complete uninstall cleanup and try again."
                )
            else:
                _set_last_error("Failed to remove some dependency files:\n" + "\n".join(failed[:20]))
            _log("Dependency uninstall completed with warnings", Qgis.Warning)
            return False

        _log(f"Dependency uninstall completed: removed {removed} entries", Qgis.Info)
        clear_runtime_restart_required()
        return True
    except Exception as exc:
        _set_last_error(str(exc))
        _log(f"Dependency uninstall failed: {exc}", Qgis.Critical)
        return False


def interactive_install_dependencies(parent=None, force_reinstall: bool = False) -> bool:
    """
    Show interactive installer dialog with real-time progress.
    
    Smart flow:
    1. Check if dependencies already import correctly
    2. If yes: Skip dialog, mark as installed, return True
    3. If no: Show dialog with real-time progress
    
    Returns True if installation succeeds, False otherwise.
    """
    _set_last_error(None)

    # Smart check: skip installer only if dependencies and core plugin imports are healthy.
    # For repair action, force_reinstall=True bypasses this shortcut.
    _activate_vendor_paths()
    if not force_reinstall:
        deps_ok = check_dependencies()
        imports_ok, import_err = _check_plugin_import_health() if deps_ok else (False, None)
        if deps_ok and imports_ok:
            _log("Dependencies already properly installed, skipping installer dialog")
            mark_as_installed()
            return True
        if deps_ok and not imports_ok and import_err:
            _set_last_error(import_err)
            _log(import_err, Qgis.Warning)
            return False
    else:
        _log("Force reinstall requested: bypassing healthy-dependencies shortcut", Qgis.Info)

    # Dependencies missing or broken - show interactive installer
    _log("Dependencies missing, showing installer dialog", Qgis.Info)

    # Define the installation callback
    def _do_install(progress_callback=None) -> bool:
        if progress_callback:
            progress_callback("Preparing installation...\n")

        if progress_callback:
            progress_callback("Preparing runtime folders before reinstall...\n")
        preflight_ok, install_targets = _preflight_runtime_reinstall(
            progress_callback=progress_callback
        )
        if not preflight_ok:
            return False

        if progress_callback:
            progress_callback("Starting package installation...\n")
        _log("Attempting interactive runtime dependency installation...", Qgis.Info)

        try:
            success = _install_via_pip(
                DEFAULT_PACKAGES,
                progress_callback=progress_callback,
                targets=install_targets,
            )

            if progress_callback:
                progress_callback("\nVerifying installation...\n")
            _activate_vendor_paths()
            importlib.invalidate_caches()
            deps_ready = check_dependencies()
            imports_ready, import_err = _check_plugin_import_health() if deps_ready else (False, None)
            if deps_ready and imports_ready:
                if progress_callback:
                    progress_callback(
                        "Dependencies were installed and verified successfully.\n"
                    )
                _log("Interactive runtime installation successful", Qgis.Info)
                mark_as_installed()
                return True
            if import_err:
                _set_last_error(import_err)
                _log(import_err, Qgis.Warning)
                if progress_callback:
                    progress_callback(f"{import_err}\n")
            else:
                verify_err = get_last_bootstrap_error() or "Dependency verification failed after install."
                if progress_callback:
                    progress_callback(f"{verify_err}\n")

            _log("Interactive runtime installation failed", Qgis.Warning)
            if not get_last_bootstrap_error():
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
            mark_runtime_restart_required("runtime dependencies installed (interactive)")
            _log("First-time installation completed successfully", Qgis.Info)
        else:
            _log("First-time installation failed or cancelled", Qgis.Warning)

        return success

    except Exception as exc:
        _log(f"Error launching installer dialog: {exc}", Qgis.Critical)
        _set_last_error(f"Installer dialog failed: {exc}")
        # Fallback to silent install
        return install_dependencies(parent, quiet=False)
