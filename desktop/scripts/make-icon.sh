#!/bin/bash

set -euo pipefail

DESKTOP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_ICON="${DESKTOP_DIR}/../frontend/public/brand/tracelog-icon-transparent-1024.png"
BUILD_DIR="${DESKTOP_DIR}/build"
OUTPUT_ICON="${BUILD_DIR}/icon.icns"

if [[ -n "${TRACELOG_PYTHON:-}" ]]; then
  "${TRACELOG_PYTHON}" "${DESKTOP_DIR}/scripts/make_icon.py" "${SOURCE_ICON}" "${OUTPUT_ICON}"
else
  conda run -n tracelog python "${DESKTOP_DIR}/scripts/make_icon.py" "${SOURCE_ICON}" "${OUTPUT_ICON}"
fi

echo "Created ${OUTPUT_ICON}"
