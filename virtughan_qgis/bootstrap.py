# virtughan_qgis/bootstrap.py
import importlib
import os
import platform
import site
import subprocess
import shutil
import sys
import glob

from qgis.core import Qgis, QgsMessageLog
from qgis.PyQt.QtWidgets import (
    QDialog,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

PKG_NAME = "virtughan"
_LAST_BOOTSTRAP_ERROR = None


def _log(msg, level=Qgis.Info):
    QgsMessageLog.logMessage(f"VirtuGhan Bootstrap: {msg}", "VirtuGhan", level)


def _set_last_error(msg: str | None):
    global _LAST_BOOTSTRAP_ERROR
    _LAST_BOOTSTRAP_ERROR = (msg or "").strip() or None


def get_last_bootstrap_error() -> str | None:
    return _LAST_BOOTSTRAP_ERROR


def _activate_user_site():
    try:
        us = site.getusersitepackages()
        if us and us not in sys.path and os.path.isdir(us):
            sys.path.insert(0, us)
            _log(f"Added user site-packages to sys.path: {us}")
    except Exception:
        pass


def check_dependencies():
    _activate_user_site()
    try:
        import virtughan

        _log("VirtuGhan package found")
        return True
    except ImportError:
        _log("VirtuGhan package not found", Qgis.Warning)
        return False


def _get_safe_python_executable():
    def _is_python_binary(path: str | None) -> bool:
        if not path:
            return False
        name = os.path.basename(path).lower()
        if platform.system() == "Windows":
            return name == "python.exe" or name.startswith("python")
        return name.startswith("python")

    candidates: list[str] = []

    if getattr(sys, "executable", None):
        candidates.append(sys.executable)

    for attr in ("_base_executable",):
        value = getattr(sys, attr, None)
        if value:
            candidates.append(value)

    for attr in ("prefix", "base_prefix", "exec_prefix", "base_exec_prefix"):
        value = getattr(sys, attr, None)
        if not value:
            continue
        if platform.system() == "Windows":
            candidates.append(os.path.join(value, "python.exe"))
        else:
            candidates.append(os.path.join(value, "bin", "python3"))
            candidates.append(os.path.join(value, "bin", "python"))

    # QGIS on Windows often sets sys.executable to qgis-ltr-bin.exe.
    if platform.system() == "Windows" and getattr(sys, "executable", None):
        exe_name = os.path.basename(sys.executable).lower()
        if "qgis" in exe_name:
            qgis_bin_dir = os.path.dirname(sys.executable)
            qgis_root = os.path.dirname(qgis_bin_dir)
            candidates.extend(glob.glob(os.path.join(qgis_root, "apps", "Python*", "python.exe")))

    for cmd in ("python", "python3"):
        resolved = shutil.which(cmd)
        if resolved:
            candidates.append(resolved)

    seen = set()
    for candidate in candidates:
        if not candidate:
            continue
        normalized = os.path.normpath(candidate)
        if normalized in seen:
            continue
        seen.add(normalized)
        if os.path.isfile(normalized) and _is_python_binary(normalized):
            _log(f"Selected Python executable: {normalized}")
            return normalized

    _log("Falling back to 'python' from PATH", Qgis.Warning)
    return "python"


def _run_subprocess(cmd: list[str], timeout: int = 240):
    kwargs = {
        "capture_output": True,
        "text": True,
        "timeout": timeout,
        "cwd": os.path.expanduser("~"),
    }

    if platform.system() == "Windows":
        try:
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        except Exception:
            pass

    return subprocess.run(cmd, **kwargs)


def _ensure_pip_available(python_exe: str) -> tuple[bool, str | None]:
    try:
        check = _run_subprocess([python_exe, "-m", "pip", "--version"], timeout=30)
        if check.returncode == 0:
            return True, None
    except Exception:
        pass

    try:
        _log("pip not available; trying ensurepip")
        ep = _run_subprocess([python_exe, "-m", "ensurepip", "--upgrade"], timeout=120)
        if ep.returncode != 0:
            detail = (ep.stderr or ep.stdout or "").strip()
            return False, f"ensurepip failed: {detail}"
    except Exception as e:
        return False, f"ensurepip exception: {e}"

    try:
        up = _run_subprocess([python_exe, "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"], timeout=180)
        if up.returncode != 0:
            _log("pip upgrade failed; continuing with current pip", Qgis.Warning)
    except Exception:
        pass

    try:
        check2 = _run_subprocess([python_exe, "-m", "pip", "--version"], timeout=30)
        if check2.returncode == 0:
            return True, None
    except Exception:
        pass

    return False, "pip is unavailable in QGIS Python environment"


def _try_install_virtughan():
    python_exe = _get_safe_python_executable()

    _log(f"Platform: {platform.system()}")
    _log(f"Python executable: {python_exe}")

    pip_ok, pip_err = _ensure_pip_available(python_exe)
    if not pip_ok:
        return False, pip_err or "pip unavailable"

    in_venv = hasattr(sys, "real_prefix") or (
        hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix
    )

    base_cmd = [python_exe, "-m", "pip", "install", PKG_NAME]
    install_commands = []
    if in_venv:
        install_commands.append(base_cmd)
    else:
        install_commands.append(base_cmd + ["--user"])
        install_commands.append(base_cmd)
    install_commands.append(base_cmd + ["--no-cache-dir"])

    errors: list[str] = []

    for i, cmd in enumerate(install_commands):
        try:
            _log(f"Trying installation method {i + 1}: {' '.join(cmd)}")
            result = _run_subprocess(cmd, timeout=300)

            if result.returncode == 0:
                _activate_user_site()
                importlib.invalidate_caches()
                _log(f"Installation successful with method {i + 1}")
                return True, None
            else:
                _log(f"Method {i + 1} failed with return code {result.returncode}")
                detail = (result.stderr or result.stdout or "").strip()
                if detail:
                    errors.append(f"Method {i + 1}: {detail}")

        except subprocess.TimeoutExpired:
            _log(f"Method {i + 1} timed out")
            errors.append(f"Method {i + 1}: timeout")
            continue
        except FileNotFoundError:
            _log(f"Method {i + 1} failed: command not found")
            errors.append(f"Method {i + 1}: command not found")
            continue
        except Exception as e:
            _log(f"Method {i + 1} failed with exception: {str(e)}")
            errors.append(f"Method {i + 1}: {e}")
            continue

    error_msg = "All installation methods failed"
    if errors:
        error_msg += "\n\n" + "\n\n".join(errors[-3:])
    return False, error_msg


def install_dependencies(parent=None, quiet=False):
    _set_last_error(None)
    if check_dependencies():
        return True

    _log("Starting dependency installation")

    if not quiet and parent:
        reply = QMessageBox.question(
            parent,
            "Install VirtuGhan?",
            "VirtuGhan package not found. Try automatic installation?\n\n"
            "This will attempt to install the package using pip.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )

        if reply != QMessageBox.Yes:
            _log("User declined installation")
            return False

    try:
        success, error = _try_install_virtughan()

        if success and check_dependencies():
            _set_last_error(None)
            if not quiet and parent:
                QMessageBox.information(
                    parent,
                    "Success",
                    "VirtuGhan installed successfully!\n\nPlease restart QGIS to ensure proper functionality.",
                )
            _log("Installation completed successfully")
            return True
        else:
            _log(f"Installation failed: {error}", Qgis.Warning)
            _set_last_error(error or "Dependency installation failed")

    except Exception as e:
        _log(f"Installation error: {str(e)}", Qgis.Critical)
        _set_last_error(str(e))
        if not quiet and parent:
            QMessageBox.warning(
                parent,
                "Installation Error",
                f"An error occurred during installation:\n{str(e)}",
            )

    if not quiet and parent:
        _show_manual_install_dialog(parent)

    return False


def _show_manual_install_dialog(parent):
    try:
        dialog = QDialog(parent)
        dialog.setWindowTitle("Manual Installation Required")
        dialog.setMinimumSize(650, 400)

        layout = QVBoxLayout()

        is_windows = platform.system() == "Windows"

        if is_windows:
            instruction_text = """Automatic installation failed. Please install manually:

WINDOWS - Method 1 (OSGeo4W Shell):
1. Open OSGeo4W Shell as Administrator
2. Run: python -m pip install virtughan

WINDOWS - Method 2 (Command Prompt):
1. Open Command Prompt as Administrator
2. Run: python -m pip install virtughan --user

WINDOWS - Method 3 (QGIS Python Console):
1. In QGIS, go to Plugins > Python Console
2. Run: import subprocess; subprocess.run(['python', '-m', 'pip', 'install', 'virtughan', '--user'])

After installation, restart QGIS completely.
"""
        else:
            instruction_text = """Automatic installation failed. Please install manually:

LINUX/MAC - Method 1:
python3 -m pip install virtughan --break-system-packages

LINUX/MAC - Method 2:
python3 -m pip install virtughan --user

LINUX/MAC - Method 3 (if using conda):
conda install -c conda-forge pip
pip install virtughan

After installation, restart QGIS.
"""

        text_edit = QTextEdit()
        text_edit.setPlainText(instruction_text)
        text_edit.setReadOnly(True)
        layout.addWidget(text_edit)

        ok_button = QPushButton("OK")
        ok_button.clicked.connect(dialog.accept)
        layout.addWidget(ok_button)

        dialog.setLayout(layout)
        dialog.exec_()

    except Exception as e:
        _log(f"Error showing manual install dialog: {str(e)}", Qgis.Warning)


def ensure_virtughan_installed(parent=None, quiet=True):
    try:
        return install_dependencies(parent, quiet)
    except Exception as e:
        _log(f"Bootstrap error: {str(e)}", Qgis.Critical)
        _set_last_error(str(e))
        return check_dependencies()
