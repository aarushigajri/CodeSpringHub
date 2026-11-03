#!/usr/bin/env bash
# Start the CodeSpringHub portal:   ./portal.sh            (reuses your private link)
#                                   ./portal.sh --new-link (old link stops working)
set -euo pipefail
HUB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="$HUB/env/bin:$PATH"

# Rebuild the web app if any frontend source is newer than the last build.
if [ ! -f "$HUB/portal/dist/index.html" ] || \
   [ -n "$(find "$HUB/portal/src" "$HUB/portal/index.html" "$HUB/portal/package.json" -newer "$HUB/portal/dist/index.html" -print -quit)" ]; then
  echo "Building the portal web app (first run or code changed)…"
  (cd "$HUB/portal" && { [ -d node_modules ] || npm ci --no-audit --no-fund; } && npm run build --silent) >/dev/null
fi

exec codespringhub-portal "$@"
