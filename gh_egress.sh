#!/usr/bin/env bash
set -euo pipefail

# ===== config =====
OWNER="yuwanyue"
REPO="1"
WF="egress-fetch.yml"
: "${GITHUB_TOKEN:?export GITHUB_TOKEN first}"

if [[ $# -lt 1 ]]; then
echo "Usage: $0 <url> [method] [body_text]"
exit 1
fi

URL="$1"
METHOD="${2:-GET}"
BODY_TEXT="${3:-}"

api() {
local method="$1"; shift
local url="$1"; shift
curl -sS -X "$method" \
-H "Authorization: Bearer $GITHUB_TOKEN" \
-H "Accept: application/vnd.github+json" \
"$url" "$@"
}

# 1) default branch
repo_json="$(api GET "https://api.github.com/repos/$OWNER/$REPO")"
BRANCH="$(python3 - <<'PY' "$repo_json"
import json,sys
print(json.loads(sys.argv[1]).get("default_branch","main"))
PY
)"

# 2) dispatch
if [[ -n "$BODY_TEXT" ]]; then
BODY_B64="$(printf "%s" "$BODY_TEXT" | base64 -w0)"
else
BODY_B64=""
fi

payload="$(python3 - <<'PY' "$BRANCH" "$URL" "$METHOD" "$BODY_B64"
import json,sys
print(json.dumps({
"ref": sys.argv[1],
"inputs": {
"url": sys.argv[2],
"method": sys.argv[3],
"body_b64": sys.argv[4]
}
}))
PY
)"

api POST "https://api.github.com/repos/$OWNER/$REPO/actions/workflows/$WF/dispatches" -d "$payload" >/dev/null
echo "[+] dispatched"

# 3) get latest run id
RUN_ID=""
for _ in $(seq 1 20); do
runs_json="$(api GET "https://api.github.com/repos/$OWNER/$REPO/actions/workflows/$WF/runs?event=workflow_dispatch&per_page=1")"
RUN_ID="$(python3 - <<'PY' "$runs_json"
import json,sys
runs=json.loads(sys.argv[1]).get("workflow_runs",[])
print(runs[0]["id"] if runs else "")
PY
)"
[[ -n "$RUN_ID" ]] && break
sleep 2
done
[[ -n "$RUN_ID" ]] || { echo "[-] no run id"; exit 1; }
echo "[+] run_id=$RUN_ID"

# 4) wait completion
while true; do
rjson="$(api GET "https://api.github.com/repos/$OWNER/$REPO/actions/runs/$RUN_ID")"
status="$(python3 - <<'PY' "$rjson"
import json,sys
d=json.loads(sys.argv[1]); print(d.get("status",""))
PY
)"
conclusion="$(python3 - <<'PY' "$rjson"
import json,sys
d=json.loads(sys.argv[1]); print(d.get("conclusion",""))
PY
)"
ech
o " $status:$conclusion"
[[ "$status" == "completed" ]] && break
sleep 3
done
[[ "$conclusion" == "success" ]] || { echo "[-] workflow failed"; exit 1; }

# 5) download release asset by tag run-<run_id>
TAG="run-$RUN_ID"
rel_json="$(api GET "https://api.github.com/repos/$OWNER/$REPO/releases/tags/$TAG")"
ASSET_URL="$(python3 - <<'PY' "$rel_json"
import json,sys
d=json.loads(sys.argv[1]); a=d.get("assets",[])
print(a[0]["browser_download_url"] if a else "")
PY
)"
[[ -n "$ASSET_URL" ]] || { echo "[-] no asset url"; exit 1; }

OUT="out_$RUN_ID"
mkdir -p "$OUT"
curl -sS -L "$ASSET_URL" -o "$OUT/result.tgz"

tar -xzf "$OUT/result.tgz" -C "$OUT"
echo "[+] done: $OUT"
echo "---- status_code ----"
cat "$OUT/status_code.txt" 2>/dev/null || true
echo "---- headers (top 40) ----"
sed -n '1,40p' "$OUT/headers.txt" 2>/dev/null || true
echo "---- body preview ----"
python3 - <<'PY' "$OUT/body.bin"
import sys
b=open(sys.argv[1],"rb").read(1000)
print(b.decode("utf-8","replace"))
PY
