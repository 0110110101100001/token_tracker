#!/usr/bin/env bash
# Register this tool's hooks in ~/.claude/settings.json.
#
#   ./install.sh                 register the Stop and SessionStart hooks
#   ./install.sh --autostart     also bring the panel up on login
#   ./install.sh --uninstall     remove both hooks and the autostart entry
#   ./install.sh --help          every flag
#
# The work is in cost_meter/install.py, which reuses this project's own atomic
# JSON write rather than reimplementing it. Unlike the hooks it installs, this
# script is run by hand and is allowed to fail loudly.
set -euo pipefail
cd "$(dirname "$0")"
exec pixi run --frozen python -m cost_meter.install "$@"
