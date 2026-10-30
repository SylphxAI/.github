#!/usr/bin/env bash
# Dependencies and Unity activation were prepared by the consumer.
set -euo pipefail
root="${GITHUB_WORKSPACE:?workspace is required}"
action="${GITHUB_ACTION_PATH:?action path is required}"
test -d "${LICENSE_HOME:?licensed HOME is required}"
test -n "${UNITY_IMAGE:?existing Unity image is required}"
plan="$(python3 "$action/prepare.py" --workspace "$root" --project "$PROJECT_PATH" --output "$COMPILE_OUTPUT" --target "$COMPILE_TARGET")"
project="$(PLAN="$plan" python3 -c 'import json,os; print(json.loads(os.environ["PLAN"])["project"])')"
output="$(PLAN="$plan" python3 -c 'import json,os; print(json.loads(os.environ["PLAN"])["output"])')"
receipt="$root/$project/$output/compile-receipt.json"
# A stale successful receipt must not survive a failed compilation.
rm -f "$receipt"
docker run --rm --workdir /workspace \
  --volume "$root:/workspace" --volume "$LICENSE_HOME:/root" \
  --env SYLPHX_COMPILE_TARGET="$COMPILE_TARGET" \
  --env SYLPHX_COMPILE_OUTPUT="/workspace/$project/$output" \
  --env PROJECT_PATH="/workspace/$project" \
  --ulimit nofile=8192:8192 --entrypoint /bin/bash "$UNITY_IMAGE" -lc '
    exec unity-editor -batchmode -nographics -quit -logFile /dev/stdout \
      -projectPath "$PROJECT_PATH" -buildTarget "$SYLPHX_COMPILE_TARGET" \
      -executeMethod SylphxCompilePlayerScripts.Run
  '
test -s "$receipt"
python3 - "$receipt" "$COMPILE_TARGET" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
assert data["target"] == sys.argv[2], "compilation target mismatch"
assert data["assemblies"], "no compiled assemblies"
assert all(len(a["sha256"]) == 64 for a in data["assemblies"]), "invalid assembly digest"
print("Verified player C# receipt:", data["target"], data["unityVersion"], len(data["assemblies"]), "assemblies")
PY
echo "receipt=$receipt" >> "${GITHUB_OUTPUT:?}"
