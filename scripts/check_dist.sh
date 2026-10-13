#!/usr/bin/env bash
# Release build guard (S12c): sdist → wheel builds, and neither artifact
# carries a file git does not track. Run it in the release checkout itself:
# local pilot configs (configs/.gitignore) exist only there.
#
#   bash scripts/check_dist.sh
set -euo pipefail

cd "$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
readonly OUT_DIR="$(mktemp -d)"
trap 'rm -rf "${OUT_DIR}"' EXIT

uv build --quiet --out-dir "${OUT_DIR}"
sdist="$(ls "${OUT_DIR}"/*.tar.gz)"
wheel="$(ls "${OUT_DIR}"/*.whl)"

# Every sdist member is a tracked file (hatch adds PKG-INFO and .gitignore).
untracked="$(
    comm -23 \
        <(tar --quoting-style=literal -tzf "${sdist}" | sed 's|^[^/]*/||' | grep -v '/$' | grep -vxE 'PKG-INFO|\.gitignore' | sort) \
        <(git ls-files -z | tr '\0' '\n' | sort)
)"
if [[ -n "${untracked}" ]]; then
    echo "check_dist: sdist carries untracked files:" >&2
    echo "${untracked}" >&2
    exit 1
fi

# The wheel ships the package and its licence only.
foreign="$(
    unzip -Z1 "${wheel}" | grep -vE '^ehr_simulator/|^ehr_simulator-[^/]+\.dist-info/' || true
)"
if [[ -n "${foreign}" ]]; then
    echo "check_dist: wheel carries files outside the package:" >&2
    echo "${foreign}" >&2
    exit 1
fi

unzip -Z1 "${wheel}" | grep -qx 'ehr_simulator/ingestion/data/geneva_units.json'
unzip -Z1 "${wheel}" | grep -qE '^ehr_simulator-[^/]+\.dist-info/licenses/LICENSE$'
echo "check_dist: ok"
