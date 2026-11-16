#!/usr/bin/env sh
# kindle-dash hook: fetch a new dashboard image and write it to "$1".
#
# The image is rendered by GitHub Actions (.github/workflows/render.yml) and
# published on this repo's `dashboard` branch.
URL="https://raw.githubusercontent.com/steelflint/kindle_weather/dashboard/dash.png"

# --check-status: a failed download exits non-zero, so kindle-dash keeps the
# last good image on screen instead of showing an error.
# --timeout: don't let a hung connection stall the refresh loop.
"$(dirname "$0")/../xh" -d -q --check-status --timeout 60 -o "$1" get "$URL"
