#!/usr/bin/env python3
"""
Bundle pip as a pure-Python wheel into the plugin vendor directory.

The wheel file (.whl) is a zip archive containing only Python source code —
no executables, no .exe files. At runtime, pip is imported directly from the
wheel via zipimport/sys.path manipulation.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

from virtughan_qgis.dependency_versions import PIP_VERSION

DEFAULT_TARGET = Path("virtughan_qgis") / "vendor"
PIP_WHEEL_FILENAME = f"pip-{PIP_VERSION}-py3-none-any.whl"
PIP_WHEEL_URL = f"https://files.pythonhosted.org/packages/py3/p/pip/{PIP_WHEEL_FILENAME}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bundle pip wheel into the plugin vendor directory for runtime installs.",
    )
    parser.add_argument(
        "--target",
        default=str(DEFAULT_TARGET),
        help="Target vendor directory (default: virtughan_qgis/vendor).",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Delete existing vendor/pip before download.",
    )
    parser.add_argument(
        "--pip-version",
        default=None,
        help="Pinned pip version to bundle (defaults to centralized config).",
    )
    return parser.parse_args()


def download_pip_wheel(target_dir: Path, version: str | None = None) -> Path:
    """Download pip wheel from PyPI into target_dir. Returns path to the wheel file."""
    ver = version or PIP_VERSION
    wheel_name = f"pip-{ver}-py3-none-any.whl"

    target_dir.mkdir(parents=True, exist_ok=True)
    wheel_path = target_dir / wheel_name

    if wheel_path.exists():
        print(f"Wheel already exists: {wheel_path}")
        return wheel_path

    # Use pip download (most reliable method across platforms)
    print(f"Downloading pip=={ver} wheel...")
    try:
        subprocess.run(
            [
                sys.executable, "-m", "pip", "download",
                "--no-deps", "--only-binary", ":all:",
                "--dest", str(target_dir),
                f"pip=={ver}",
            ],
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        # Fallback: try direct URL download from PyPI
        print(f"pip download failed ({exc}), trying direct URL...")
        wheel_url = f"https://files.pythonhosted.org/packages/py3/p/pip/{wheel_name}"
        try:
            urllib.request.urlretrieve(wheel_url, str(wheel_path))
        except Exception as url_exc:
            raise RuntimeError(
                f"Could not download pip wheel. Tried pip download and direct URL.\n"
                f"pip error: {exc}\nURL error: {url_exc}"
            ) from url_exc

    # pip download may produce a slightly different filename, find it
    if not wheel_path.exists():
        candidates = sorted(target_dir.glob(f"pip-{ver}*.whl"))
        if candidates:
            wheel_path = candidates[0]
        else:
            raise FileNotFoundError(f"Could not find downloaded pip wheel for version {ver}")

    print(f"Downloaded: {wheel_path} ({wheel_path.stat().st_size} bytes)")
    return wheel_path


def main() -> int:
    args = parse_args()

    vendor_root = Path(args.target).resolve()
    pip_dir = vendor_root / "pip"

    if args.clean:
        print(f"Cleaning pip directory: {pip_dir}")
        shutil.rmtree(pip_dir, ignore_errors=True)

    pip_dir.mkdir(parents=True, exist_ok=True)

    version = args.pip_version or PIP_VERSION
    wheel_path = download_pip_wheel(pip_dir, version)

    # Remove any old installed pip files (from previous approach)
    for item in pip_dir.iterdir():
        if item.is_dir():
            print(f"Removing old pip installation directory: {item}")
            shutil.rmtree(item, ignore_errors=True)

    print(f"\nDone. Pip wheel bundled at: {wheel_path}")
    print("Runtime packages (virtughan + deps) are installed automatically by bootstrap on first plugin run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
