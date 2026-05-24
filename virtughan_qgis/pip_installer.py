#!/usr/bin/env python3
"""Runtime pip installer for QGIS plugin dependencies."""

from __future__ import annotations

import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Callable


_LAST_INSTALL_ERROR: str | None = None

_INSTALL_LOG_SUPPRESS_MARKERS = (
    "earth-search.aws.element84.com/v1/search",
    "falling back to a dummysession",
    "cached path failed",
    "retrying uncached tile generation",
    "http request: post https://earth-search.aws.element84.com/v1/search",
    "ttp request: post https://earth-search.aws.element84.com/v1/search",
)


def _set_last_install_error(message: str | None):
    global _LAST_INSTALL_ERROR
    _LAST_INSTALL_ERROR = (message or "").strip() or None


def get_last_install_error() -> str | None:
    return _LAST_INSTALL_ERROR


def _is_unrelated_runtime_noise(line: str) -> bool:
    lower = (line or "").strip().lower()
    if not lower:
        return False
    return any(marker in lower for marker in _INSTALL_LOG_SUPPRESS_MARKERS)


def _detect_lock_related_error(lines: list[str]) -> str | None:
    combined = "\n".join(lines)
    if not combined:
        return None

    lock_markers = [
        "WinError 5",
        "Access is denied",
        "PermissionError",
        "being used by another process",
        "Permission denied",
        "[Errno 13]",
        "EACCES",
        "Operation not permitted",
    ]
    if any(marker in combined for marker in lock_markers):
        return (
            "Dependency files appear to be locked or permission denied. "
            "Please restart QGIS, then retry installation."
        )
    return None


def _bundled_pip_root() -> Path | None:
    """Return path to the bundled pip wheel (.whl) if present."""
    plugin_root = Path(__file__).resolve().parent
    pip_dir = plugin_root / "vendor" / "pip"
    if not pip_dir.is_dir():
        return None

    # Look for a pip wheel file
    wheels = list(pip_dir.glob("pip-*.whl"))
    if wheels:
        return wheels[0]  # Return the wheel path

    # Legacy fallback: check for installed pip package directory
    if (pip_dir / "pip").is_dir():
        return pip_dir

    return None


class _CallbackStream:
    """File-like stream that forwards complete lines to a callback."""

    def __init__(self, callback: Callable[[str], None] | None):
        self._callback = callback
        self._buffer = ""

    def write(self, data: str):
        if not self._callback or not data:
            return
        self._buffer += data
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                self._callback(line)

    def flush(self):
        if self._callback and self._buffer.strip():
            self._callback(self._buffer.strip())
        self._buffer = ""

    def isatty(self) -> bool:
        """Return False to indicate this is not a terminal."""
        return False

    def readable(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False


def _resolve_embedded_python_executable() -> str:
    """Return a real Python interpreter path for embedded-QGIS contexts."""
    candidates: list[Path] = []

    if sys.prefix:
        candidates.extend(
            [
                Path(sys.prefix) / "python.exe",
                Path(sys.prefix) / "bin" / "python.exe",
                Path(sys.prefix) / "bin" / "python3",
                Path(sys.prefix) / "bin" / "python",
                Path(sys.prefix) / "python3",
                Path(sys.prefix) / "python",
            ]
        )

    # Linux: check common QGIS-bundled Python locations
    if sys.platform == "linux":
        candidates.extend(
            [
                Path("/usr/bin/python3"),
                Path("/usr/bin/python"),
            ]
        )
        # Some Linux distros bundle Python inside the QGIS prefix
        if sys.prefix:
            candidates.extend(
                [
                    Path(sys.prefix) / "bin" / "python3.12",
                    Path(sys.prefix) / "bin" / "python3.11",
                    Path(sys.prefix) / "bin" / "python3.10",
                ]
            )

    if sys.platform == "darwin" and sys.executable:
        exe_path = Path(sys.executable).resolve()
        for parent in exe_path.parents:
            if parent.name == "MacOS":
                app_contents = parent.parent
                candidates.extend(
                    [
                        parent / "bin" / "python3",
                        app_contents / "Frameworks" / "Python.framework" / "Versions" / "Current" / "bin" / "python3",
                        app_contents / "Frameworks" / "bin" / "python3",
                        app_contents / "Resources" / "python" / "bin" / "python3",
                    ]
                )
                break
        # Also check Homebrew QGIS Python on macOS
        candidates.extend(
            [
                Path("/usr/local/bin/python3"),
                Path("/opt/homebrew/bin/python3"),
            ]
        )

    if sys.executable:
        candidates.append(Path(sys.executable))

    seen: set[str] = set()
    for candidate in candidates:
        normalized = str(candidate)
        if normalized in seen:
            continue
        seen.add(normalized)

        name = candidate.name.lower()
        if sys.platform == "darwin" and name == "qgis":
            continue
        if candidate.is_file() and "python" in name:
            return normalized

    for candidate in candidates:
        normalized = str(candidate)
        if sys.platform == "darwin" and Path(normalized).name.lower() == "qgis":
            continue
        if candidate.is_file():
            return normalized

    return sys.executable


def _run_pip_inprocess(
    target_dir: Path,
    package_specs: list[str],
    pip_root: Path | None,
    progress_callback: Callable[[str], None] | None,
) -> bool:
    """Run pip inside the current Python process (preferred in QGIS)."""
    _set_last_install_error(None)

    added_path = False
    pip_path_str = None
    if pip_root:
        # pip_root can be either a .whl file or a directory
        pip_path_str = str(pip_root)
        if pip_path_str not in sys.path:
            sys.path.insert(0, pip_path_str)
            added_path = True

    try:
        from pip._internal.cli.main import main as pip_main
    except Exception as exc:
        if progress_callback:
            progress_callback(f"Could not import bundled pip in-process: {exc}")
        if added_path and pip_path_str:
            try:
                sys.path.remove(pip_path_str)
            except Exception:
                pass
        return False

    pip_args = [
        "install",
        "--disable-pip-version-check",
        "--upgrade",
        "--ignore-installed",
        "--prefer-binary",
        "--target",
        str(target_dir),
        *package_specs,
    ]

    # On Windows, enforce binary-only for rasterio (avoids needing C compiler).
    # On Linux/macOS, allow source builds as fallback since pre-built wheels
    # may not be available for all platform/Python version combinations.
    if sys.platform == "win32":
        pip_args.insert(5, "rasterio")
        pip_args.insert(5, "--only-binary")

    if progress_callback:
        progress_callback("Using in-process pip execution")

    captured_lines: list[str] = []

    def _emit(line: str):
        captured_lines.append(line)
        if progress_callback and not _is_unrelated_runtime_noise(line):
            progress_callback(line)

    stream = _CallbackStream(_emit)
    original_executable = sys.executable
    embedded_python = _resolve_embedded_python_executable()
    try:
        if embedded_python and embedded_python != original_executable:
            sys.executable = embedded_python
            if progress_callback:
                progress_callback(f"Using embedded Python executable: {embedded_python}")
        with redirect_stdout(stream), redirect_stderr(stream):
            result_code = pip_main(pip_args)
        stream.flush()
        if result_code != 0 and not get_last_install_error():
            _set_last_install_error(_detect_lock_related_error(captured_lines))
        return result_code == 0
    except Exception as exc:
        if progress_callback:
            progress_callback(f"In-process pip error: {exc}")
        if not get_last_install_error():
            detected = _detect_lock_related_error(captured_lines)
            _set_last_install_error(detected or f"In-process pip error: {exc}")
        return False
    finally:
        sys.executable = original_executable
        if added_path and pip_path_str:
            try:
                sys.path.remove(pip_path_str)
            except Exception:
                pass


def install_dependencies(
    target_dir: Path | str,
    package_specs: list[str],
    progress_callback: Callable[[str], None] | None = None,
) -> bool:
    """
    Install packages using QGIS's in-process Python.
    
    Args:
        target_dir: Installation target directory
        package_specs: List of pip package specifications
        progress_callback: Optional callback to receive real-time log lines
    
    Returns:
        True if installation succeeded, False otherwise
    """
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    pip_root = _bundled_pip_root()

    if progress_callback:
        progress_callback(f"Installing: {', '.join(package_specs)}\n")

    # Run pip in-process using QGIS's Python (3.12 in fresh installs)
    return _run_pip_inprocess(target_dir, package_specs, pip_root, progress_callback)
