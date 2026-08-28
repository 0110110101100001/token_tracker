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


def sounds_dir():
    """Where the celebration's sounds live.

    Under the project rather than under home(): these ship with the code and are
    the same on every machine, while home() is the ledger -- a per-machine
    directory every test redirects and a user could reasonably delete. A sound
    that vanished with the ledger would be a feature that switched itself off.
    """
    return project_root() / "sounds"


def sound_path(name):
    return sounds_dir() / name


def usage_path():
    """Where our own read of the account's limits lands.

    Ours rather than Claude Code's: cost_meter/usage_api.py asks the server the
    same question Claude Code asks and writes the answer here, in the same shape,
    so utilization.read() has one parser for both. Under home() rather than beside
    Claude Code's own file, because it is this tool's data and every test
    redirects that directory already.
    """
    return home() / "usage.json"


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


def widget_output_path():
    """Where the panel's own stdout and stderr land.

    Separate from log_path() because the two have different authors and
    different lifetimes. That file is this tool's own account of what it
    decided, one line per decision, and it is meant to be kept. This one is
    whatever GTK, pixi and the interpreter said on the panel's way up -- almost
    always nothing, occasionally a traceback -- and it is started again once it
    outgrows a session's worth.

    It exists because of a failure that had no trace anywhere. On Windows the
    panel runs under pythonw, which a detached process leaves with no console,
    so a crash during startup wrote its traceback to a discarded handle. The log
    said the spawn succeeded -- it had -- and the panel simply never appeared,
    which from the outside is indistinguishable from a hook that never ran.
    """
    return home() / "widget-output.log"


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


def claude_config_path():
    """Claude Code's own config file — a sibling of claude_home(), not inside it.

    This is where Claude Code caches what the server says about the account's
    limits, which is the one thing the transcripts cannot tell us: a limit
    belongs to the account, and a transcript only records this machine.

    Overridable on its own rather than derived from claude_home(), for the reason
    that function already gives and one more. Here the consequence of deriving it
    would be worse than a confusing test: claude_home() is redirected in every
    test, so a derived path would never point at the real file, and the reader
    would look permanently empty rather than wrong.
    """
    override = os.environ.get("COST_METER_CLAUDE_CONFIG")
    return Path(override) if override else Path.home() / ".claude.json"
