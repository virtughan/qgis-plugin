#!/usr/bin/env pwsh

$ErrorActionPreference = "Stop"

# Use the active Python directly (works inside .venv or conda)
python vendor_deps.py --clean
python build.py
