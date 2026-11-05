#!/bin/bash
# downloads-cleanup.sh -- deletes any Wrike-attachment download older than
# 48h from ~/levi-webui/downloads/ (2026-10-21, Devin: "i want to be able
# to retrieve drawings"). These can be real internal structural/CAD
# drawings -- don't let them accumulate on disk indefinitely past their
# stated link lifetime. Matches the per-token directory shape tools.py's
# wrike_download_attachment creates: downloads/<ns>/<token>/<filename>.
set -euo pipefail
DOWNLOADS_DIR="$HOME/levi-webui/downloads"
[ -d "$DOWNLOADS_DIR" ] || exit 0
find "$DOWNLOADS_DIR" -mindepth 2 -maxdepth 2 -type d -mmin +2880 -exec rm -rf {} +
