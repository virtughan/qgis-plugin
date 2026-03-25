#!/usr/bin/env python3

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


PLUGIN_NAME = "virtughan_qgis"
BUILD_DIR = "build"
DIST_DIR = "dist"
ZIP_NAME = "virtughan-qgis-plugin.zip"


def ensure_vendored_dependencies(root: Path) -> None:
    """
    Validate that either:
    1. virtughan is pre-vendored (offline mode), OR
    2. pip is bundled (for runtime installation)
    """
    plugin_root = root / PLUGIN_NAME
    
    # Check for pre-vendored virtughan
    prevendored = [
        plugin_root / "vendor" / "virtughan",
        plugin_root / "libs" / "virtughan",
    ]
    
    # Check for bundled pip
    pip_path = plugin_root / "vendor" / "pip"
    
    if any(path.is_dir() for path in prevendored):
        return
    
    if pip_path.is_dir():
        return
    
    # If neither exists, fail
    raise FileNotFoundError(
        "Missing vendored dependencies.\n\n"
        "Either:\n"
        "  1. Pre-vendor virtughan: python vendor_deps.py --clean\n"
        "  2. Or bundle pip: python vendor_deps.py --clean\n\n"
        "Expected one of:\n"
        f"  - {prevendored[0]}\n"
        f"  - {prevendored[1]}\n"
        f"  - {pip_path} (for runtime pip installation)"
    )


def remove_build_artifacts(path: Path) -> None:
    for item in path.rglob("*"):
        if item.name == "__pycache__" and item.is_dir():
            shutil.rmtree(item, ignore_errors=True)
        elif item.suffix in {".pyc", ".pyo"} and item.is_file():
            item.unlink(missing_ok=True)
        elif item.name == ".DS_Store" and item.is_file():
            item.unlink(missing_ok=True)


def main() -> int:
    root = Path(__file__).resolve().parent
    build_dir = root / BUILD_DIR
    dist_dir = root / DIST_DIR
    plugin_build_dir = build_dir / PLUGIN_NAME
    zip_path = dist_dir / ZIP_NAME

    print("Building QGIS plugin package...")

    print("Validating vendored dependencies...")
    ensure_vendored_dependencies(root)

    shutil.rmtree(build_dir, ignore_errors=True)
    shutil.rmtree(dist_dir, ignore_errors=True)
    plugin_build_dir.mkdir(parents=True, exist_ok=True)
    dist_dir.mkdir(parents=True, exist_ok=True)

    print("Generating metadata.txt from pyproject.toml...")
    subprocess.run([sys.executable, "generate_metadata.py"], cwd=root, check=True)

    print("Copying plugin files...")
    shutil.copytree(root / PLUGIN_NAME, plugin_build_dir, dirs_exist_ok=True)

    print("Copying license and documentation...")
    for license_name in ("LICENSE.txt", "LICENSE"):
        license_path = root / license_name
        if license_path.exists():
            shutil.copy2(license_path, plugin_build_dir / license_name)
            break

    static_dir = root / "static"
    if static_dir.exists():
        shutil.copytree(static_dir, plugin_build_dir / "static", dirs_exist_ok=True)

    print("Cleaning build artifacts...")
    remove_build_artifacts(build_dir)

    print("Creating zip package...")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in plugin_build_dir.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(build_dir))

    print("Build complete!")
    print(f"Plugin package: {DIST_DIR}/{ZIP_NAME}")
    print("Install in QGIS: Plugins > Manage and Install Plugins > Install from ZIP")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
