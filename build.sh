#!/usr/bin/env bash

set -e

python3 vendor_deps.py --clean
python3 build.py
