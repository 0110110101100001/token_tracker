# cost_meter/paths.py
"""Every filesystem location the tool touches, in one place.

Both directories are environment-overridable so tests never read or write the
user's real data.
"""

import os
from pathlib import Path


def project_root():
    return Path(__file__).resolve().parent.parent


def home():
    override = os.environ.get("COST_METER_HOME")
    return Path(override) if override else project_root() / "data"


def events_path():
    return home() / "events.jsonl"


def state_path():
    return home() / "state.json"


def offsets_path():
    return home() / "offsets.json"


def session_marks_path():
    return home() / "session_marks.json"


def config_path():
    return home() / "config.json"


def lock_path():
    return home() / "tally.lock"


def pid_path():
    return home() / "widget.pid"


def widget_lock_path():
    """The lock a running panel holds for its whole life.

    Separate from lock_path(), which serialises tally runs against each other: a
    panel holds this one continuously, so sharing the file would wedge every
    Stop hook behind it. Separate from pid_path() as well, because the panel
    writes that file and this one is only ever locked, never read.
    """
    return home() / "widget.lock"


def log_path():
    return home() / "cost-meter.log"


def pricing_path():
    return project_root() / "pricing.json"


def transcripts_root():
    override = os.environ.get("COST_METER_TRANSCRIPTS")
    return Path(override) if override else Path.home() / ".claude" / "projects"


def claude_home():
    """Claude Code's own directory — not this tool's.

    Overridable on its own rather than derived from transcripts_root(): that one
    points at a tree of transcripts and is redirected in tests to a directory
    holding nothing else, while this one is read for credentials and settings.
    Tying them together would mean a test that wanted one would silently get
    the other.
    """
    override = os.environ.get("COST_METER_CLAUDE_HOME")
    return Path(override) if override else Path.home() / ".claude"


def credentials_path():
    return claude_home() / ".credentials.json"


def claude_settings_path():
    return claude_home() / "settings.json"
