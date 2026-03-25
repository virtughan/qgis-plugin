#!/usr/bin/env pwsh

$ErrorActionPreference = "Stop"

if (Get-Command uv -ErrorAction SilentlyContinue) {
    uv run python vendor_deps.py --clean
    uv run python build.py
}
elseif (Get-Command python -ErrorAction SilentlyContinue) {
    python vendor_deps.py --clean
    python build.py
}
else {
    throw "Python is required to run build.py"
}
