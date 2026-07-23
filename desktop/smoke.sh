#!/bin/bash

set -euo pipefail

DESKTOP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE_BIN="${1:-${DESKTOP_DIR}/dist/engine/tracelog-engine/tracelog-engine}"
SMOKE_DATA_DIR="$(mktemp -d "${TMPDIR:-/tmp}/tracelog-smoke.XXXXXX")"
ENGINE_LOG="${SMOKE_DATA_DIR}/engine.log"
ENGINE_PID=""

cleanup() {
  if [[ -n "${ENGINE_PID}" ]] && kill -0 "${ENGINE_PID}" 2>/dev/null; then
    kill -TERM "${ENGINE_PID}"
    wait "${ENGINE_PID}" 2>/dev/null || true
  fi
  if [[ "${SMOKE_DATA_DIR}" == *"/tracelog-smoke."* ]]; then
    rm -rf "${SMOKE_DATA_DIR}"
  fi
}
trap cleanup EXIT INT TERM

if [[ ! -x "${ENGINE_BIN}" ]]; then
  echo "Frozen engine not found: ${ENGINE_BIN}" >&2
  exit 1
fi

TRACELOG_DATA_DIR="${SMOKE_DATA_DIR}" "${ENGINE_BIN}" >"${ENGINE_LOG}" 2>&1 &
ENGINE_PID=$!

PORT=""
for _ in {1..150}; do
  if ! kill -0 "${ENGINE_PID}" 2>/dev/null; then
    cat "${ENGINE_LOG}" >&2
    exit 1
  fi
  PORT="$(sed -n 's/^TRACELOG_PORT=//p' "${ENGINE_LOG}" | tail -n 1)"
  if [[ -n "${PORT}" ]]; then
    break
  fi
  sleep 0.2
done

if [[ -z "${PORT}" ]]; then
  cat "${ENGINE_LOG}" >&2
  echo "Timed out waiting for TRACELOG_PORT" >&2
  exit 1
fi

BASE_URL="http://127.0.0.1:${PORT}"
for _ in {1..150}; do
  if curl --silent --fail "${BASE_URL}/api/health" >/dev/null; then
    break
  fi
  sleep 0.2
done

curl --silent --fail "${BASE_URL}/api/health" >/dev/null
curl --silent --fail "${BASE_URL}/" | grep --quiet --ignore-case "<!doctype html>"
sqlite3 "${SMOKE_DATA_DIR}/workspace/state.db" \
  "INSERT INTO posts(id, ts, content, importance, created_at, updated_at) VALUES ('desktop-smoke', '2026-07-23T12:00:00+08:00', '冻结检索冒烟', 0.5, 1.0, 1.0); INSERT INTO post_events(post_id, job_id, event_type, payload_json, created_at) VALUES ('desktop-smoke', NULL, 'pipeline_done', '{\"smoke\":true}', 1.0);"
curl --silent --fail --get --data-urlencode "q=冻结检索" "${BASE_URL}/api/posts/search" | grep --quiet "desktop-smoke"
curl --silent --fail --get --data-urlencode "q=冻结检索" --data-urlencode "mode=hybrid" "${BASE_URL}/api/posts/search" | grep --quiet "desktop-smoke"
curl --silent --fail --max-time 5 "${BASE_URL}/api/posts/desktop-smoke/events?after_id=0" | grep --quiet "event: pipeline_done"

echo "Frozen engine smoke test passed on port ${PORT}."
