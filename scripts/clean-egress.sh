#!/usr/bin/env bash
set -euo pipefail

ROOT="${1:-.}"
ROOT="$(cd "$ROOT" && pwd)"

find "$ROOT" -maxdepth 1 -type d -name 'out_*' -print -exec rm -rf {} +
find "$ROOT" -type d \( -name '__pycache__' -o -name '.pytest_cache' \) -print -exec rm -rf {} +
