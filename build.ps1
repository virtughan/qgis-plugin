#!/usr/bin/env pwsh

$ErrorActionPreference = "Stop"

if (Get-Command uv -ErrorAction SilentlyContinue) {
    uv run python build.py
}
elseif (Get-Command python -ErrorAction SilentlyContinue) {
    python build.py
}
else {
    throw "Python is required to run build.py"
}
