#!/usr/bin/env bash
# README quickstart smoke (S12c test 21): fresh clone → `uv sync --locked`
# → `serve --demo` → sign in → synth_001 page carries the demo banner.
#
# Clones the committed HEAD of this repository (not GitHub, which may not
# have the commit yet), so uncommitted changes are not exercised.
#
#   bash scripts/quickstart_smoke.sh            # port 8765
#   QUICKSTART_PORT=9000 bash scripts/quickstart_smoke.sh
set -euo pipefail

readonly REPO_DIR="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
readonly PORT="${QUICKSTART_PORT:-8765}"
readonly BASE_URL="http://127.0.0.1:${PORT}"
readonly BOOT_TIMEOUT_SECONDS=60
readonly WORK_DIR="$(mktemp -d)"

server_pid=""

cleanup() {
    if [[ -n "${server_pid}" ]]; then
        kill "${server_pid}" 2>/dev/null || true
        wait "${server_pid}" 2>/dev/null || true
    fi
    rm -rf "${WORK_DIR}"
}
trap cleanup EXIT

# A server already on the port would answer the polls below instead of ours.
if curl -s -o /dev/null "${BASE_URL}/"; then
    echo "quickstart smoke: port ${PORT} is in use; set QUICKSTART_PORT" >&2
    exit 1
fi

# The README path, verbatim apart from the clone source.
git clone --quiet "file://${REPO_DIR}" "${WORK_DIR}/ehr_simulator"
cd "${WORK_DIR}/ehr_simulator"
uv sync --locked
uv run ehr-simulator serve --demo --port "${PORT}" >"${WORK_DIR}/serve.log" 2>&1 &
server_pid=$!

# Wait until the server answers; fail with its log otherwise.
for _ in $(seq "${BOOT_TIMEOUT_SECONDS}"); do
    if curl -fsS -o /dev/null "${BASE_URL}/login"; then
        break
    fi
    if ! kill -0 "${server_pid}" 2>/dev/null; then
        cat "${WORK_DIR}/serve.log"
        echo "quickstart smoke: server exited during boot" >&2
        exit 1
    fi
    sleep 1
done
curl -fsS -o /dev/null "${BASE_URL}/login" || {
    cat "${WORK_DIR}/serve.log"
    echo "quickstart smoke: server not answering after ${BOOT_TIMEOUT_SECONDS}s" >&2
    exit 1
}

# Sign in, then open the first synthetic patient as a clinician would.
jar="${WORK_DIR}/cookies.txt"
curl -fsS -o /dev/null -c "${jar}" -d "clinician_name=Quickstart" "${BASE_URL}/login"
page="$(curl -fsS -b "${jar}" "${BASE_URL}/patient/synth_001/timepoint/0")"

grep -q 'class="demo-banner"' <<<"${page}"
grep -q 'synth_001' <<<"${page}"
echo "quickstart smoke: ok"
