#!/usr/bin/env bash
# Run the widget as an X11 client under XWayland.
#
# A Wayland client may not position itself or raise itself above others, which
# is exactly what this widget needs. Under XWayland both work normally.
set -euo pipefail
cd "$(dirname "$0")"
export GDK_BACKEND=x11
exec python3 widget.py "$@"
