#!/bin/bash
# Builds dist/Tracklister.app on macOS. Run: ./build_mac.sh   (add --zip for a release zip)
set -euo pipefail
cd "$(dirname "$0")"
[ -d .venv ] || { uv venv .venv --python 3.12 && uv pip install --python .venv/bin/python -r requirements.txt pyinstaller; }
.venv/bin/python build.py "$@"
