#!/usr/bin/env python3
"""Runtime pip installer for QGIS plugin dependencies."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _bundled_pip_root() -> Path | None:
    """Return bundled pip root (vendor/pip) if present."""
    plugin_root = Path(__file__).resolve().parent
    candidate = plugin_root / "virtughan_qgis" / "vendor" / "pip"
    if (candidate / "pip").is_dir():
        return candidate

    # Also support running from plugin folder directly
    candidate_alt = plugin_root / "vendor" / "pip"
    if (candidate_alt / "pip").is_dir():
        return candidate_alt

    return None


def install_dependencies(target_dir: Path | str, package_specs: list[str], verbose: bool = False) -> bool:
    """Install packages to target directory using bundled pip when available."""
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    pip_root = _bundled_pip_root()
    if pip_root:
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(pip_root) + (os.pathsep + existing_pythonpath if existing_pythonpath else "")

    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--upgrade",
        "--target",
        str(target_dir),
        *package_specs,
    ]

    if verbose:
        print(f"Running: {' '.join(cmd)}", file=sys.stderr)

    try:
        subprocess.run(
            cmd,
            check=True,
            capture_output=not verbose,
            text=True,
            env=env,
        )
        return True
    except subprocess.CalledProcessError as exc:
        if verbose:
            print(f"pip install failed: {exc}", file=sys.stderr)
            if exc.stdout:
                print(exc.stdout, file=sys.stderr)
            if exc.stderr:
                print(exc.stderr, file=sys.stderr)
        return False
    except Exception as exc:
        if verbose:
            print(f"Error running pip: {exc}", file=sys.stderr)
        return False


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Runtime pip installer for plugin dependencies")
    parser.add_argument("--target", required=True, help="Target directory for pip install --target")
    parser.add_argument("--packages", nargs="+", required=True, help="Package specs to install")
    parser.add_argument("--verbose", action="store_true", help="Show pip output")

    args = parser.parse_args()
    success = install_dependencies(args.target, args.packages, args.verbose)
    sys.exit(0 if success else 1)
