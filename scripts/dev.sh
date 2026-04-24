#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cmd="${1:-help}"

print_env_check() {
  for key in GITHUB_TOKEN CHANNEL_OWNER CHANNEL_REPO CHANNEL_EGRESS_WORKFLOW CHANNEL_EGRESS_REF CHANNEL_EGRESS_OUTPUT_DIR; do
    if [[ -n "${!key:-}" ]]; then
      printf '%s=%s\n' "$key" "set"
    else
      printf '%s=%s\n' "$key" "unset"
    fi
  done
}

print_recent_egress() {
  local archive_root="${CHANNEL_EGRESS_OUTPUT_DIR:-$ROOT/egress_archive}"
  local index_file="$archive_root/index.jsonl"
  if [[ ! -f "$index_file" ]]; then
    echo "no egress index found: $index_file"
    exit 0
  fi
  python3 - <<'PY' "$index_file"
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
for row in rows[-10:]:
    print(
        f"{row.get('recorded_at','')}  "
        f"{row.get('request_id','')}  "
        f"{row.get('mode','')}  "
        f"{row.get('run_id','')}  "
        f"{row.get('local_output_dir','')}"
    )
PY
}

case "$cmd" in
  env-check)
    print_env_check
    ;;
  recent-egress)
    print_recent_egress
    ;;
  help|*)
    cat <<'EOF'
Usage:
  bash ./scripts/dev.sh env-check
  bash ./scripts/dev.sh recent-egress
EOF
    ;;
esac
