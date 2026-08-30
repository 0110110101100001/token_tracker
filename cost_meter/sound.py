"""Which sound a turn has earned, and how to make it. No GTK.

The audible half of what Patrik mode does visually: a turn lands, and the panel
says so. Off by default and on its own switch, because a sound is a different
decision from an animation -- one plays on your screen, the other plays in
whatever room you are sitting in.

Separate from widget.py for the reason cost_meter/patrik.py is: *which file* a
session has earned is arithmetic, and arithmetic can be tested without a sound
card. The widget owns the menu item and the flag; this module owns the choice
and the spawning.

Everything here is a decoration, and the rule the overlay already follows applies
without exception: a decoration must never take the meter down. There is no path
out of `play` that raises. A box with no player, a file somebody deleted, a
sound server that has gone away -- each is silence and a line in the log, and the
figures carry on updating.

WAV, and only WAV. On Windows the player is `winsound`, which is in the standard
library and plays nothing else; the alternative was a GStreamer stack for the
sake of MP3, and every DLL added to this project on Windows is another file for
Smart App Control to have no opinion about. The panel has already died once that
way.
"""

import array
import math
import os
import shutil
import struct
import subprocess
import wave

from . import log, paths

# The base, for a turn that has not reached the first step.
BASE_FILE = "under-10.wav"
# And the steps, read from the top down: the first threshold the turn has passed
# wins.
#
# The same three figures as `patrik.RATE_TIERS`, but no longer the same idea, and
# nothing asserts they agree any more. The rate tiers are read against the
# session's running total and these against one turn, so the two now answer
# different questions and only happen to step on the same numbers. Rescaling
# either of them is a change to that one alone; a test tying them together would
# have made a sound rescale look like a glyph regression.
TIERS = ((30.0, "over-30.wav"), (20.0, "over-20.wav"), (10.0, "over-10.wav"))

# What to play with on anything that is not Windows, in the order to try. paplay
# first because PipeWire ships a drop-in for it and PulseAudio is what it was
# written for, so it is the one most likely to be there; aplay last because it
# talks to ALSA directly and will happily seize a device a sound server is using.
PLAYERS = ("paplay", "pw-play", "aplay")

# Placeholder synthesis. A sine per note, at CD rate so no player has to resample.
SAMPLE_RATE = 44100
AMPLITUDE = 0.35
NOTE_SECONDS = 0.12
# A fifth of each note spent fading out. Cut a sine off mid-cycle and the step to
# silence is a click, which on four files played all day is the sort of detail
# that makes somebody switch the feature off without knowing why.
FADE_SHARE = 0.2
# Rising, so the tiers are told apart by ear rather than by filename: one note
# for a quiet session, four for an expensive one, each series a step higher.
NOTES = (
    (660.0,),
    (660.0, 880.0),
    (660.0, 880.0, 1100.0),
    (660.0, 880.0, 1100.0, 1320.0),
)


def file_for(turn_usd):
    """The file a turn costing `turn_usd` has earned.

    The turn, not the session: this is the figure on the panel's top row, and
    the one the person who just pressed enter is waiting on. A session total can
    only climb, so keyed to it the sound would ratchet up once and stay there for
    the rest of the day, saying the same thing about every turn from a two-cent
    one to a twenty-dollar one. `patrik.rate` is the channel that reads the
    session, and it still does.

    A missing figure is the quiet sound rather than an error: state.json can
    carry a null there and the panel must not die of it.

    No absolute value, unlike `patrik.duration_ms` and exactly like
    `patrik.rate`: a correction to -$40 is a turn that refunded, not one that
    spent, and the loudest sound would be the panel reading the sign backwards.
    """
    turn_usd = turn_usd or 0.0
    for threshold, name in TIERS:
        if turn_usd >= threshold:
            return name
    return BASE_FILE


def player():
    """The command to play a WAV with, or None if there is nothing to play with.

    None is a first-class answer rather than a failure, as `build_overlay`
    returning no overlay is: a machine with no sound stack is not a broken
    panel, and the meter is still a meter without a noise.

    Windows never reaches here -- `play` uses winsound, which is always present.
    """
    for name in PLAYERS:
        found = shutil.which(name)
        if found:
            return [found]
    return None


def play(path):
    """Play `path`, if it exists and there is anything to play it with.

    Never raises and never waits. Both halves are load-bearing: this is called
    from `refresh` on the GTK main loop, so an exception here would be a turn
    whose figures stopped updating because a sound failed, and a blocking call
    would freeze the panel for the length of the sound.
    """
    try:
        if not os.path.isfile(path):
            log.write(f"sound: no such file {path}")
            return
        if os.name == "nt":
            import winsound

            # ASYNC so the panel is not held for the length of the sound, and
            # NODEFAULT so a file Windows cannot read is silence rather than the
            # system ding -- an unexplained beep every turn is worse than none.
            winsound.PlaySound(str(path),
                               winsound.SND_FILENAME
                               | winsound.SND_ASYNC
                               | winsound.SND_NODEFAULT)
            return
        command = player()
        if command is None:
            log.write(f"sound: no player found, tried {', '.join(PLAYERS)}")
            return
        # Output discarded rather than inherited: the panel on Windows runs under
        # pythonw with no console at all, and a player writing to a closed handle
        # is a failure with nothing to gain from it.
        subprocess.Popen(command + [str(path)],
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL)
    except Exception as error:                      # noqa: BLE001 - see above
        log.write(f"sound: {type(error).__name__}: {error}")


def play_for(turn_usd):
    """Play whatever the turn that just landed has earned. The panel's one entry."""
    play(paths.sound_path(file_for(turn_usd)))


def _tone(frequencies):
    """`frequencies` played one after another, as 16-bit mono samples."""
    samples = array.array("h")
    note_frames = int(SAMPLE_RATE * NOTE_SECONDS)
    fade_frames = max(1, int(note_frames * FADE_SHARE))
    for frequency in frequencies:
        for frame in range(note_frames):
            fade = min(1.0, (note_frames - frame) / fade_frames)
            value = math.sin(2.0 * math.pi * frequency * frame / SAMPLE_RATE)
            samples.append(int(value * fade * AMPLITUDE * 32767))
    return samples


def make_placeholders(directory=None):
    """Write the four placeholder sounds. Returns the paths written.

    Placeholders, but generated rather than found: four short files nobody can
    point at the origin of would be four files nobody can regenerate, relicense
    or explain. This is `pixi run sounds`, and swapping in real recordings is
    dropping four WAVs over the top.
    """
    directory = paths.sounds_dir() if directory is None else directory
    directory.mkdir(parents=True, exist_ok=True)
    names = [BASE_FILE] + [name for _, name in reversed(TIERS)]
    written = []
    for name, frequencies in zip(names, NOTES):
        path = directory / name
        with wave.open(str(path), "w") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(SAMPLE_RATE)
            samples = _tone(frequencies)
            handle.writeframes(struct.pack(f"<{len(samples)}h", *samples))
        written.append(path)
    return written


if __name__ == "__main__":
    for written in make_placeholders():
        print(written)
