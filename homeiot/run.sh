#!/usr/bin/env bash
# Start the dashboard from anywhere: homeiot/run.sh [--demo] [--port 8712] ...
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 -m homeiot "$@"
