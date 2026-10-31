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
echo "Player C# output: /workspace/$project/$output; host receipt: $receipt"
set +e
docker run --rm --workdir /workspace \
  --volume "$root:/workspace" --volume "$LICENSE_HOME:/root" \
  --env SYLPHX_COMPILE_TARGET="$COMPILE_TARGET" \
  --env SYLPHX_COMPILE_OUTPUT="/workspace/$project/$output" \
  --env PROJECT_PATH="/workspace/$project" \
  --ulimit nofile=65536:65536 --entrypoint /bin/bash "$UNITY_IMAGE" -lc '
    printf "Unity container open-file limits: soft=%s hard=%s\n" "$(ulimit -Sn)" "$(ulimit -Hn)"
    exec unity-editor -batchmode -nographics -quit -logFile /dev/stdout \
      -projectPath "$PROJECT_PATH" -buildTarget "$SYLPHX_COMPILE_TARGET" \
      -executeMethod SylphxCompilePlayerScripts.Run
  '
status=$?
set -e
echo "Unity player C# process exit: $status"
if [ "$status" -ne 0 ]; then exit "$status"; fi
if [ ! -s "$receipt" ]; then
  echo "::error::Unity exited without a persistent receipt at $receipt"
  find "$root/$project/Builds" -maxdepth 3 -type f -name 'compile-receipt.json' -print 2>/dev/null || true
  exit 1
fi
python3 - "$receipt" "$COMPILE_TARGET" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
assert data["target"] == sys.argv[2], "compilation target mismatch"
assert data["assemblies"], "no compiled assemblies"
assert all(len(a["sha256"]) == 64 for a in data["assemblies"]), "invalid assembly digest"
print("Verified player C# receipt:", data["target"], data["unityVersion"], len(data["assemblies"]), "assemblies")
PY
echo "receipt=$receipt" >> "${GITHUB_OUTPUT:?}"
