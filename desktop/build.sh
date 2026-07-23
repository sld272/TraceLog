#!/bin/bash

set -euo pipefail

DESKTOP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${DESKTOP_DIR}/.." && pwd)"

cd "${PROJECT_ROOT}"

npm --prefix frontend run build

if ! conda run -n tracelog python -c "import PyInstaller" >/dev/null 2>&1; then
  echo "PyInstaller is missing from conda environment 'tracelog'." >&2
  echo "Install it with: conda run -n tracelog python -m pip install -r desktop/requirements-build.txt" >&2
  exit 1
fi

conda run -n tracelog python -m PyInstaller \
  --clean \
  --noconfirm \
  --distpath "${DESKTOP_DIR}/dist/engine" \
  --workpath "${DESKTOP_DIR}/build/pyinstaller" \
  "${DESKTOP_DIR}/engine.spec"

"${DESKTOP_DIR}/smoke.sh"
"${DESKTOP_DIR}/scripts/make-icon.sh"

if [[ -f "${DESKTOP_DIR}/package-lock.json" ]]; then
  npm --prefix desktop ci
else
  npm --prefix desktop install
fi

npm --prefix desktop run dist:mac

echo "Desktop artifacts are in ${DESKTOP_DIR}/dist/shell"
