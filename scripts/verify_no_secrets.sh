#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if find "$ROOT" \( -name ".env" -o -name "*.bak*" -o -name "*.bad*" -o -name "*.pristine*" \) | grep -q .; then
  echo "Found forbidden env/backup files" >&2
  find "$ROOT" \( -name ".env" -o -name "*.bak*" -o -name "*.bad*" -o -name "*.pristine*" \) >&2
  exit 1
fi

secret_files="$(grep -RIlE \
  "(sk-[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|Authorization:[[:space:]]*Bearer[[:space:]]+[A-Za-z0-9._-]{20,})" \
  "$ROOT" \
  --exclude-dir=.git \
  --exclude-dir=.baseline || true)"
if [[ -n "$secret_files" ]]; then
  echo "Found likely secret material in $(printf '%s\n' "$secret_files" | wc -l | tr -d ' ') files" >&2
  printf '%s\n' "$secret_files" >&2
  exit 1
fi

echo "secret scan passed"
