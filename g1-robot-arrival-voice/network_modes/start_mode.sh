#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
# With only wired/wifi supplied this prints a plan. --run is always explicit.
exec python3 session.py "$@"
