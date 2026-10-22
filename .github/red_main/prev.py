from datetime import datetime
import json
import sys

def timestamp(value):
    if not isinstance(value, str) or not value:
        raise ValueError("workflow history timestamp unavailable")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("workflow history timestamp lacks timezone")
    return parsed

created = timestamp(sys.argv[1])
body = json.load(sys.stdin)
rows = body.get("workflow_runs") if isinstance(body, dict) else None
count = body.get("total_count") if isinstance(body, dict) else None
if (not isinstance(rows, list) or type(count) is not int or count < 0
        or count != len(rows)):
    raise ValueError("workflow history unavailable or incomplete")
runs = []
for row in rows:
    if (not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] < 1
            or row.get("status") not in
            ("completed", "queued", "in_progress", "waiting", "pending", "requested")):
        raise ValueError("workflow history identity unavailable")
    at = timestamp(row.get("created_at"))
    if (row["status"] == "completed" and row.get("conclusion") not in
            ("success", "failure", "timed_out", "cancelled", "skipped", "neutral", "action_required", "startup_failure", "stale")):
        raise ValueError("workflow history conclusion unavailable")
    if (row["status"] == "completed" and at < created
            and row.get("conclusion") in ("success", "failure", "timed_out")):
        runs.append(row)
runs.sort(key=lambda row: timestamp(row["created_at"]), reverse=True)
if runs:
    print(f"{runs[0]['id']}\t{runs[0]['conclusion']}\t{runs[0].get('html_url', '')}")
