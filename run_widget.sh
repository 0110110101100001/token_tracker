#!/usr/bin/env bash
# Start the panel from outside the pixi environment.
#
# This is the entry point for anything that is not already inside pixi: an
# autostart entry, or a shell. It adds nothing but the environment --
# GDK_BACKEND=x11 belongs to the `widget` task in pixi.toml, so the task and
# this script cannot drift apart. (The SessionStart hook takes the other road,
# through cost_meter/launch.py, which starts the panel only if none is up.)
#
# --frozen installs nothing and never touches the network: the environment is
# used exactly as pixi.lock describes it, or the run fails. Re-run `pixi install`
# after editing pixi.toml.
set -euo pipefail
cd "$(dirname "$0")"
exec pixi run --frozen widget "$@"
