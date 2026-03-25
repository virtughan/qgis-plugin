#!/usr/bin/env python3

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from virtughan_qgis.dependency_versions import pip_package_spec

DEFAULT_TARGET = Path("virtughan_qgis") / "vendor"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bundle pip into the plugin vendor directory for runtime installs.",
    )
    parser.add_argument(
        "--target",
        default=str(DEFAULT_TARGET),
        help="Target vendor directory (default: virtughan_qgis/vendor).",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Delete existing vendor/pip before install.",
    )
    parser.add_argument(
        "--python",
        dest="python_exe",
        default=sys.executable,
        help="Python executable to use for pip (default: current interpreter).",
    )
    parser.add_argument(
        "--pip-version",
        default=None,
        help="Pinned pip version to bundle (defaults to centralized config).",
    )
    return parser.parse_args()


def run_pip_install(python_exe: str, target: Path, package_spec: str) -> None:
    cmd = [
        python_exe,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "--target",
        str(target),
        package_spec,
    ]
    print("Running:", " ".join(cmd))
    subprocess.run(cmd, check=True)


def main() -> int:
    args = parse_args()

    vendor_root = Path(args.target).resolve()
    pip_target = vendor_root / "pip"

    vendor_root.mkdir(parents=True, exist_ok=True)

    if args.clean:
        print(f"Cleaning pip target: {pip_target}")
        shutil.rmtree(pip_target, ignore_errors=True)

    pip_target.mkdir(parents=True, exist_ok=True)

    package_spec = f"pip=={args.pip_version}" if args.pip_version else pip_package_spec()
    print(f"Bundling pip into: {pip_target}")

    try:
        run_pip_install(args.python_exe, pip_target, package_spec)
    except subprocess.CalledProcessError as exc:
        print(f"Failed to bundle pip: {exc}")
        return 1

    print("Done.")
    print("Runtime packages (virtughan + deps) are installed automatically by bootstrap on first plugin run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
