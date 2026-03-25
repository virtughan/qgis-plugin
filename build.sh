#!/usr/bin/env bash

set -e

if command -v uv >/dev/null 2>&1; then
    uv run python vendor_deps.py --clean
    uv run python build.py
else
    python3 vendor_deps.py --clean
    python3 build.py
fi
