# tests/test_sound.py
"""Which sound a turn gets, and what plays it.

Split from the panel for the reason tests/test_patrik.py gives for the glyph
maths: *which file* a session has earned is arithmetic, and arithmetic needs
neither a sound card nor a display. Only the wiring below needs a panel.

Nothing in this file ever actually makes a noise. Every test that reaches `play`
replaces the spawn, and that is not merely politeness towards whoever runs the
suite: `paplay` on a headless box blocks until it can reach a server, and a suite
that sat there waiting on PulseAudio would fail as a timeout with no hint of why.
"""

import os
import time
import unittest
import unittest.mock
import wave
from datetime import datetime

import widget
from cost_meter import launch, patrik, paths, sound, store
from tests.support import TempHome

HAS_DISPLAY = launch.has_display()

if HAS_DISPLAY:
    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk


class TierTest(unittest.TestCase):
    """What the turn cost picks the file, in four steps."""

    def test_a_cheap_turn_gets_the_first_sound(self):
        self.assertEqual(sound.file_for(4.99), "under-10.wav")

    def test_each_threshold_moves_to_the_next_one(self):
        self.assertEqual(sound.file_for(10.0), "over-10.wav")
        self.assertEqual(sound.file_for(20.0), "over-20.wav")
        self.assertEqual(sound.file_for(30.0), "over-30.wav")

    def test_a_threshold_lands_on_the_figure_itself(self):
        # The row shows two decimals, so the step happens where the panel says.
        self.assertEqual(sound.file_for(9.99), "under-10.wav")
        self.assertEqual(sound.file_for(29.99), "over-20.wav")

    def test_the_top_step_is_the_ceiling(self):
        self.assertEqual(sound.file_for(5000.0), sound.file_for(30.0))

    def test_a_missing_figure_is_the_quiet_sound_rather_than_a_crash(self):
        """state.json can carry a null there, as everywhere else."""
        self.assertEqual(sound.file_for(None), "under-10.wav")

    def test_a_negative_figure_is_a_quiet_turn(self):
        # A refunded turn is not an expensive one. Same reading as `patrik.rate`,
        # and deliberately unlike `duration_ms`, where a length is a length.
        self.assertEqual(sound.file_for(-40.0), "under-10.wav")

    def test_there_are_four_of_them_and_they_are_all_different(self):
        chosen = {sound.file_for(usd) for usd in (0.0, 10.0, 20.0, 30.0)}
        self.assertEqual(len(chosen), 4)

    def test_it_is_read_against_the_turn_and_the_glyph_rate_against_the_session(self):
        """The two sets of steps are no longer one idea, and must not be tied.

        They still carry the same three figures, and an assertion that they match
        used to guard that. It is gone rather than updated: the sound now steps on
        one turn and the glyph rate on the session's running total, so the numbers
        agreeing is a coincidence and holding them equal would turn any later
        rescale of the sound into a failure claiming the glyphs had regressed.

        What replaces it is the distinction itself, on the functions rather than
        on the constants -- the same $12 is a loud turn and a slow-glyph session,
        and nothing about that follows from three numbers being equal.
        """
        self.assertEqual(sound.file_for(12.0), "over-10.wav")
        self.assertEqual(patrik.rate(12.0), patrik.rate(10.0))


class FileTest(unittest.TestCase):
    """The files themselves: shipped, playable, and not silence.

    Placeholders, but real ones. A missing or zero-length file would be
    indistinguishable on the ear from the feature being switched off, which is
    the one failure this whole module exists to make audible.
    """

    def every_file(self):
        return [name for _, name in sound.TIERS] + [sound.BASE_FILE]

    def test_every_tier_has_a_file_on_disk(self):
        for name in self.every_file():
            with self.subTest(name=name):
                self.assertTrue(paths.sound_path(name).is_file(), name)

    def test_they_are_wav_files_a_player_can_open(self):
        # winsound plays WAV and nothing else, so this is the format the feature
        # is defined in rather than one it happens to ship.
        for name in self.every_file():
            with self.subTest(name=name):
                with wave.open(str(paths.sound_path(name))) as handle:
                    self.assertGreater(handle.getnframes(), 0)
                    self.assertEqual(handle.getsampwidth(), 2)

    def test_none_of_them_is_silence(self):
        for name in self.every_file():
            with self.subTest(name=name):
                with wave.open(str(paths.sound_path(name))) as handle:
                    frames = handle.readframes(handle.getnframes())
                self.assertTrue(any(frames), name)

    def test_none_of_them_runs_long_enough_to_pile_up_all_day(self):
        """A ceiling on the files, and a deliberately loose one.

        The bound was 1.5s, and the promise behind it was that every sound is
        over before the next turn can land. `under-10.wav` is now a 3.5-second
        recording rather than a tone, so for that file the promise is suspended
        on purpose -- see the Sound section of docs/PANEL.md.

        What is left is still worth asserting: a stray file nobody meant to ship
        -- a whole track dropped in by mistake -- would have the panel playing
        over itself for the rest of the session, and on Windows each turn would
        cut the previous sound off mid-note. Put the bound back under a turn's
        length if the long clip does not survive daily use.
        """
        for name in self.every_file():
            with self.subTest(name=name):
                with wave.open(str(paths.sound_path(name))) as handle:
                    seconds = handle.getnframes() / handle.getframerate()
                self.assertLess(seconds, 4.0, name)


class PlayerTest(unittest.TestCase):
    """Finding something to play with, and surviving finding nothing."""

    def test_it_takes_the_first_player_that_is_installed(self):
        found = {"pw-play": "/usr/bin/pw-play", "aplay": "/usr/bin/aplay"}
        with unittest.mock.patch.object(sound.shutil, "which", found.get):
            self.assertEqual(sound.player(), ["/usr/bin/pw-play"])

    def test_no_player_installed_is_an_answer_rather_than_an_error(self):
        # A box with no sound stack is not a broken panel. The meter carries on
        # exactly as it does where the screen cannot composite an overlay.
        with unittest.mock.patch.object(sound.shutil, "which", lambda _: None):
            self.assertIsNone(sound.player())

    def test_the_preference_order_is_the_shipped_one(self):
        self.assertEqual(sound.PLAYERS, ("paplay", "pw-play", "aplay"))


class PlayTest(unittest.TestCase):
    """`play` is a decoration, and a decoration may never take the meter down."""

    def spawned(self):
        """Records what would have been spawned instead of spawning it."""
        return unittest.mock.patch.object(sound.subprocess, "Popen")

    def test_it_hands_the_file_to_the_player(self):
        with unittest.mock.patch.object(sound, "player",
                                        lambda: ["/usr/bin/paplay"]):
            with self.spawned() as popen:
                sound.play(paths.sound_path(sound.BASE_FILE))
        command = popen.call_args.args[0]
        self.assertEqual(command[0], "/usr/bin/paplay")
        self.assertEqual(os.path.basename(command[-1]), sound.BASE_FILE)

    def test_a_missing_file_is_silence_rather_than_a_crash(self):
        with unittest.mock.patch.object(sound, "player",
                                        lambda: ["/usr/bin/paplay"]):
            with self.spawned() as popen:
                sound.play(paths.sound_path("no-such-sound.wav"))
        popen.assert_not_called()

    def test_no_player_is_silence_rather_than_a_crash(self):
        with unittest.mock.patch.object(sound, "player", lambda: None):
            with self.spawned() as popen:
                sound.play(paths.sound_path(sound.BASE_FILE))
        popen.assert_not_called()

    def test_a_player_that_blows_up_does_not_reach_the_caller(self):
        """The panel calls this from `refresh`. An exception here would be a
        turn that stopped the figures updating because a sound failed."""
        with unittest.mock.patch.object(sound, "player",
                                        lambda: ["/usr/bin/paplay"]):
            with self.spawned() as popen:
                popen.side_effect = OSError("no such thing")
                sound.play(paths.sound_path(sound.BASE_FILE))

    def test_it_does_not_wait_for_the_sound_to_finish(self):
        # Popen rather than run: this is called from the GTK main loop, and a
        # blocking call there freezes the panel for the length of the sound.
        with unittest.mock.patch.object(sound, "player",
                                        lambda: ["/usr/bin/paplay"]):
            with self.spawned() as popen:
                sound.play(paths.sound_path(sound.BASE_FILE))
        self.assertFalse(popen.return_value.wait.called)


class PlaceholderTest(TempHome):
    """The generator behind the shipped files, so they can be made again."""

    def test_it_writes_one_file_per_tier(self):
        written = sound.make_placeholders(paths.home() / "sounds")
        self.assertEqual(sorted(path.name for path in written),
                         sorted([name for _, name in sound.TIERS]
                                + [sound.BASE_FILE]))

    def test_what_it_writes_is_playable(self):
        for path in sound.make_placeholders(paths.home() / "sounds"):
            with self.subTest(name=path.name):
                with wave.open(str(path)) as handle:
                    self.assertGreater(handle.getnframes(), 0)

    def test_a_dearer_session_gets_a_longer_sound(self):
        """The four are told apart by ear, not merely by filename."""
        directory = paths.home() / "sounds"
        sound.make_placeholders(directory)

        def frames(name):
            with wave.open(str(directory / name)) as handle:
                return handle.getnframes()

        lengths = [frames(sound.BASE_FILE)] + [
            frames(name) for _, name in reversed(sound.TIERS)]
        self.assertEqual(lengths, sorted(lengths))
        self.assertEqual(len(set(lengths)), 4)


# ---------------------------------------------------------------------------
# The wiring into the panel. Everything above needs no display; everything below
# builds a real CostMeter, as tests/test_patrik.py does for the same reason.


def a_state(written, turn_usd=0.25, session_usd=1.0):
    return {"updated_at": datetime.fromtimestamp(written).astimezone().isoformat(),
            "last_turn_usd": turn_usd,
            "session": {"id": "s1", "usd": session_usd},
            "today_usd": 1.0,
            "window_5h": {"usd": 2.0},
            "window_7d": {"usd": 3.0},
            "limits": None,
            "unknown_models": []}


class PanelTest(TempHome):
    def setUp(self):
        super().setUp()
        self.window = widget.CostMeter()
        self.window.disconnect_by_func(Gtk.main_quit)
        self.addCleanup(self.window.destroy)
        self.addCleanup(self.window.update_config,
                        lambda c: c.pop(widget.SOUND_KEY, None))
        # Nothing in the suite is allowed to make a noise; see the module note.
        patcher = unittest.mock.patch.object(widget.sound, "play")
        self.played = patcher.start()
        self.addCleanup(patcher.stop)

    def turn(self, turn_usd=0.25, session_usd=1.0):
        self._turns = getattr(self, "_turns", 0) + 1
        store.write_json_atomic(
            paths.state_path(),
            a_state(time.time() - self._turns,
                    turn_usd, session_usd))
        self.window.refresh()

    def played_file(self):
        return os.path.basename(str(self.played.call_args.args[0]))


@unittest.skipUnless(HAS_DISPLAY, "no display")
class SoundMenuTest(PanelTest):
    def captions(self):
        return [caption for caption, _ in self.window.menu_entries()]

    def test_it_sits_directly_under_patrik_mode(self):
        captions = self.captions()
        index = next(i for i, caption in enumerate(captions)
                     if caption.startswith("Set Patrik mode"))
        self.assertEqual(captions[index + 1], "Set sound on")

    def test_the_caption_says_what_the_click_will_do(self):
        self.window.set_sound(True)
        self.assertIn("Set sound off", self.captions())
        self.window.set_sound(False)
        self.assertIn("Set sound on", self.captions())

    def test_every_entry_still_has_a_handler(self):
        for caption, handler in self.window.menu_entries():
            self.assertTrue(callable(handler), caption)


@unittest.skipUnless(HAS_DISPLAY, "no display")
class SoundToggleTest(PanelTest):
    def test_it_is_off_until_it_is_asked_for(self):
        """A panel that made a noise unasked is a panel in somebody's meeting."""
        self.assertFalse(self.window.sound_enabled())

    def test_the_setting_outlives_the_panel(self):
        self.window.set_sound(True)
        second = widget.CostMeter()
        second.disconnect_by_func(Gtk.main_quit)
        self.addCleanup(second.destroy)
        self.assertTrue(second.sound_enabled())

    def test_turning_it_off_leaves_no_key_behind(self):
        self.window.set_sound(True)
        self.window.set_sound(False)
        self.assertNotIn(widget.SOUND_KEY, self.config())

    def test_only_a_literal_true_counts_as_on(self):
        # A hand-edited config with "yes" in it must not produce a panel that
        # makes a noise with no obvious way of being asked to stop.
        self.write_config({widget.SOUND_KEY: "yes"})
        self.assertFalse(self.window.sound_enabled())

    def test_toggling_it_leaves_the_other_settings_alone(self):
        self.window.update_config(
            lambda c: c.__setitem__("widget_position", [7, 9]))
        self.window.set_sound(True)
        self.window.set_sound(False)
        self.assertEqual(self.config().get("widget_position"), [7, 9])


@unittest.skipUnless(HAS_DISPLAY, "no display")
class SoundOnTurnTest(PanelTest):
    def test_a_new_turn_plays_once(self):
        self.window.set_sound(True)
        self.turn()
        self.assertEqual(self.played.call_count, 1)

    def test_a_new_turn_plays_nothing_while_it_is_off(self):
        self.turn()
        self.played.assert_not_called()

    def test_the_turns_own_cost_picks_the_file(self):
        # The session is held at a dollar throughout, so nothing here can pass
        # by reading the wrong figure and getting lucky.
        self.window.set_sound(True)
        for turn_usd, expected in ((1.0, "under-10.wav"),
                                   (10.0, "over-10.wav"),
                                   (20.0, "over-20.wav"),
                                   (30.0, "over-30.wav")):
            with self.subTest(turn_usd=turn_usd):
                self.turn(turn_usd=turn_usd, session_usd=1.0)
                self.assertEqual(self.played_file(), expected)

    def test_a_dear_session_does_not_make_a_cheap_turn_loud(self):
        """The half of the change that is easy to get wrong and never notice.

        Every turn of a long session costs the same as it would have on the
        first, so a $500 afternoon must not turn a two-cent turn into the
        loudest sound the panel owns. Held from both sides, because a `play_for`
        still wired to the session would sound perfectly reasonable all day --
        it only ever gets louder -- and would be caught by nothing else here.
        """
        self.window.set_sound(True)
        self.turn(turn_usd=0.25, session_usd=500.0)
        self.assertEqual(self.played_file(), "under-10.wav")
        self.turn(turn_usd=30.0, session_usd=0.5)
        self.assertEqual(self.played_file(), "over-30.wav")

    def test_a_repaint_that_is_not_a_turn_plays_nothing(self):
        """refresh() runs four ways and only one of them is a charge.

        The file monitor, the 60-second staleness poll and `Refresh now` all
        re-read a state.json that has not changed. A sound on any of those would
        be a panel beeping at somebody every minute all day.
        """
        self.window.set_sound(True)
        self.turn()
        self.played.reset_mock()
        self.window.refresh()
        self.played.assert_not_called()

    def test_switching_it_on_does_not_play_by_itself(self):
        self.window.set_sound(True)
        self.played.assert_not_called()

    def test_the_panel_opening_does_not_play(self):
        # Auto-launch opens a panel at every session start, and the figure
        # already on disk has not just been charged. Same trap the glyphs and
        # the counting rows each document.
        self.window.set_sound(True)
        store.write_json_atomic(paths.state_path(),
                                a_state(time.time() - 1))
        third = widget.CostMeter()
        third.disconnect_by_func(Gtk.main_quit)
        self.addCleanup(third.destroy)
        self.played.assert_not_called()

    def test_it_does_not_need_patrik_mode(self):
        """Two toggles, two decisions: a sound in an open-plan office is not the
        same question as an animation on your own screen."""
        self.window.set_sound(True)
        self.window.set_patrik(False)
        self.turn()
        self.assertEqual(self.played.call_count, 1)

    def test_patrik_mode_alone_makes_no_noise(self):
        self.window.set_patrik(True)
        self.addCleanup(self.window.update_config,
                        lambda c: c.pop(widget.PATRIK_KEY, None))
        self.turn()
        self.played.assert_not_called()


if __name__ == "__main__":
    unittest.main()
