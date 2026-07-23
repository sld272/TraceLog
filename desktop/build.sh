#!/bin/bash

set -euo pipefail

DESKTOP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${DESKTOP_DIR}/.." && pwd)"
ENGINE_EXECUTABLE="${DESKTOP_DIR}/dist/engine/tracelog-engine/tracelog-engine"

cd "${PROJECT_ROOT}"

if [[ -n "${TRACELOG_PYTHON:-}" ]]; then
  PYTHON=("${TRACELOG_PYTHON}")
else
  PYTHON=(conda run -n tracelog python)
fi

npm --prefix frontend run build

if ! "${PYTHON[@]}" -c "import PyInstaller" >/dev/null 2>&1; then
  echo "PyInstaller is missing for ${PYTHON[*]}." >&2
  echo "Install it with: ${PYTHON[*]} -m pip install -r desktop/requirements-build.txt" >&2
  exit 1
fi

"${PYTHON[@]}" -m PyInstaller \
  --clean \
  --noconfirm \
  --distpath "${DESKTOP_DIR}/dist/engine" \
  --workpath "${DESKTOP_DIR}/build/pyinstaller" \
  "${DESKTOP_DIR}/engine.spec"

"${PYTHON[@]}" "${DESKTOP_DIR}/scripts/smoke_engine.py" "${ENGINE_EXECUTABLE}"
"${DESKTOP_DIR}/scripts/make-icon.sh"

if [[ -f "${DESKTOP_DIR}/package-lock.json" ]]; then
  npm --prefix desktop ci
else
  npm --prefix desktop install
fi

npm --prefix desktop run dist:mac

shopt -s nullglob
shell_apps=("${DESKTOP_DIR}"/dist/shell/mac*/*.app)
shopt -u nullglob
if [[ ${#shell_apps[@]} -eq 0 ]]; then
  echo "Packaged .app not found under ${DESKTOP_DIR}/dist/shell" >&2
  exit 1
fi
shell_app="${shell_apps[0]}"
shell_executable="${shell_app}/Contents/MacOS/$(basename "${shell_app}" .app)"
"${PYTHON[@]}" "${DESKTOP_DIR}/scripts/smoke_shell.py" "${shell_executable}"

echo "Desktop artifacts are in ${DESKTOP_DIR}/dist/shell"
