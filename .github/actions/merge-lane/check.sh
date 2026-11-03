#!/usr/bin/env bash
set -euo pipefail
source_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
deps="${RUNNER_TEMP:?}/merge-lane-pyyaml-6.0.3"
if ! PYTHONPATH="$deps" python3 -c 'import yaml; assert yaml.__version__ == "6.0.3"' 2>/dev/null; then
  python3 -m pip install --disable-pip-version-check --no-input --only-binary=:all: \
    --target "$deps" 'PyYAML==6.0.3'
fi
PYTHONPATH="$deps" python3 "$source_dir/../workflow-lint/merge_lane_gate.py"
