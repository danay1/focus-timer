"""Deterministic checks for the single-file application's state machine and UI."""

import array
import importlib.machinery
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
import wave

SOURCE = Path(__file__).resolve().parents[1] / "focus_timer.pyw"
loader = importlib.machinery.SourceFileLoader("focus_timer", str(SOURCE))
spec = importlib.util.spec_from_loader(loader.name, loader)
module = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = module
loader.exec_module(module)
Countdown, State, TimerApp = module.Countdown, module.State, module.TimerApp


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class CountdownTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.timer = Countdown(10, self.clock)

    def test_initial_and_zero_duration(self):
        self.assertEqual(self.timer.remaining, 10)
        self.assertIs(self.timer.state, State.READY)
        self.timer.set_duration(0)
        self.assertFalse(self.timer.start())
        self.assertEqual(self.timer.progress, 0)

    def test_uses_deadline_not_number_of_ticks(self):
        self.assertTrue(self.timer.start())
        self.clock.advance(7.25)
        self.assertFalse(self.timer.update())
        self.assertAlmostEqual(self.timer.remaining, 2.75)
        self.assertEqual(self.timer.display_seconds, 3)
        self.assertAlmostEqual(self.timer.progress, .725)

    def test_pause_and_resume_preserve_fractional_seconds(self):
        self.timer.start()
        self.clock.advance(2.25)
        self.assertFalse(self.timer.pause())
        self.assertIs(self.timer.state, State.PAUSED)
        self.clock.advance(100)
        self.assertFalse(self.timer.update())
        self.assertEqual(self.timer.remaining, 7.75)
        self.timer.start()
        self.clock.advance(7.5)
        self.assertFalse(self.timer.update())
        self.assertEqual(self.timer.display_seconds, 1)
        self.clock.advance(.25)
        self.assertTrue(self.timer.update())

    def test_finishes_once_after_delayed_tick(self):
        self.timer.start()
        self.clock.advance(100)
        self.assertTrue(self.timer.update())
        self.assertIs(self.timer.state, State.FINISHED)
        self.assertEqual(self.timer.display_seconds, 0)
        self.assertEqual(self.timer.progress, 1)
        self.assertFalse(self.timer.update())
        self.assertFalse(self.timer.pause())

    def test_pause_at_deadline_finishes(self):
        self.timer.start()
        self.clock.advance(10)
        self.assertTrue(self.timer.pause())
        self.assertIs(self.timer.state, State.FINISHED)

    def test_reset_returns_to_original_duration(self):
        self.timer.start()
        self.clock.advance(4)
        self.timer.update()
        self.timer.reset()
        self.assertIs(self.timer.state, State.READY)
        self.assertEqual(self.timer.remaining, 10)
        self.assertIsNone(self.timer.deadline)

    def test_restart_after_finish(self):
        self.timer.start()
        self.clock.advance(11)
        self.timer.update()
        self.assertTrue(self.timer.start())
        self.assertEqual(self.timer.remaining, 10)

    def test_double_start_does_not_move_deadline(self):
        self.timer.start()
        deadline = self.timer.deadline
        self.clock.advance(2)
        self.assertFalse(self.timer.start())
        self.assertEqual(self.timer.deadline, deadline)

    def test_duration_validation(self):
        for invalid in (-1, module.MAX_DURATION + 1, 1.5, True, "10"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.timer.set_duration(invalid)
        self.timer.set_duration(module.MAX_DURATION)
        self.assertEqual(self.timer.display_seconds, 359999)
        self.timer.start()
        with self.assertRaises(RuntimeError):
            self.timer.set_duration(20)

    def test_duration_change_on_pause_starts_new_session(self):
        self.timer.start()
        self.clock.advance(1)
        self.timer.pause()
        self.timer.set_duration(30)
        self.assertIs(self.timer.state, State.READY)
        self.assertEqual(self.timer.remaining, 30)

    def test_input_validation(self):
        for value in ("", "0", "00", "59"):
            self.assertTrue(module.valid_part(value, 59))
        for value in ("60", "-1", "1.5", " 5", "aaa", "000", "１２", "²"):
            self.assertFalse(module.valid_part(value, 59))
        self.assertTrue(module.valid_part("99", 99))
        self.assertEqual(module.split_time(3661), (1, 1, 1))


class ChimeTests(unittest.TestCase):
    @staticmethod
    def peak(data):
        with wave.open(io.BytesIO(data), "rb") as wav:
            samples = array.array("h", wav.readframes(wav.getnframes()))
        return max(abs(value) for value in samples)

    def test_chime_is_a_long_valid_wav(self):
        data = module.build_chime_wav(70)
        with wave.open(io.BytesIO(data), "rb") as wav:
            self.assertEqual(wav.getnchannels(), 1)
            self.assertEqual(wav.getsampwidth(), 2)
            self.assertEqual(wav.getframerate(), module.CHIME_SAMPLE_RATE)
            duration = wav.getnframes() / wav.getframerate()
        self.assertGreater(duration, 6)
        self.assertGreater(self.peak(data), 0)

    def test_volume_scales_amplitude_and_zero_is_silent(self):
        quiet = self.peak(module.build_chime_wav(20))
        loud = self.peak(module.build_chime_wav(100))
        self.assertGreater(loud, quiet)
        self.assertGreater(self.peak(module.build_chime_wav(150)), quiet)
        self.assertEqual(self.peak(module.build_chime_wav(0)), 0)
        self.assertEqual(
            module.build_chime_wav(150), module.build_chime_wav(100),
        )


class UITests(unittest.TestCase):
    """Build real Tk widgets without showing a window; drive actual callbacks."""

    def setUp(self):
        self.app = TimerApp(enable_tray=False)
        self.errors = []
        self.app.report_callback_exception = lambda *error: self.errors.append(error)
        self.clock = Clock()
        self.app.timer.clock = self.clock
        # Don't play system sounds during automated tests.
        self.app.sound.set(False)

    def tearDown(self):
        self.app._quit()
        self.assertEqual(self.errors, [])

    def test_widgets_presets_controls_and_resume(self):
        app = self.app
        app._select_preset(300)
        self.assertEqual([p.get() for p in app.parts], ["00", "05", "00"])
        self.assertEqual([label.cget("text") for label in app._time_labels], ["00", "05", "00"])
        app._start()
        self.assertTrue(all(e.cget("state") == "disabled" for e in app._entries))
        self.clock.advance(1.25)
        app._pause()
        app._normalize_part(0)
        app._normalize_part(1)
        self.assertIs(app.timer.state, State.PAUSED)
        self.assertEqual(app.timer.remaining, 298.75)
        app._start()
        self.assertEqual(app.timer.remaining, 298.75)
        app._reset()
        self.assertEqual(app.timer.remaining, 300)
        self.assertEqual(app.pause_button.cget("state"), "disabled")

    def test_zero_disables_start(self):
        self.app._select_preset(0)
        self.app._start()
        self.assertIs(self.app.timer.state, State.READY)
        self.assertEqual(self.app.start_button.cget("state"), "disabled")

    def test_resize_event_uses_root_scaling(self):
        for width in (660, 740, 1000):
            self.app._resize_digits(SimpleNamespace(widget=self.app, width=width))
            size = self.app._digit_font.cget("size")
            self.assertGreaterEqual(size, 66)
            self.assertLessEqual(size, 96)

    def test_editing_paused_timer_creates_new_session(self):
        self.app._start()
        self.app._pause()
        self.app.parts[2].set("30")
        self.assertIs(self.app.timer.state, State.READY)
        self.assertEqual(self.app.timer.duration, 930)

    def test_finish_tick_alert_reset_and_replay(self):
        app = self.app
        app._select_preset(1)
        app._start()
        self.clock.advance(1)
        app.after_cancel(app._tick_job)
        app._tick()
        self.assertIs(app.timer.state, State.FINISHED)
        self.assertIsNotNone(app._alert_job)
        self.assertIn("В Р Е М Я", app.eyebrow.cget("text"))
        app._start()
        self.assertIsNone(app._alert_job)
        self.assertIs(app.timer.state, State.RUNNING)
        app._reset()

    def test_boundary_pause_alert_only_once(self):
        app = self.app
        app._select_preset(1)
        app._start()
        self.clock.advance(1)
        app._pause()
        alert_job = app._alert_job
        app._pause()
        self.assertEqual(app._alert_job, alert_job)
        self.assertIs(app.timer.state, State.FINISHED)

    def test_close_with_active_timer_minimizes_to_tray(self):
        app = self.app

        class FakeTray:
            shown = False

            def update(self, *_args):
                pass

            def show(self):
                self.shown = True

            def notify(self, _message):
                pass

        tray = FakeTray()
        app._tray = tray
        app._select_preset(30)
        app._start()
        app._close()
        self.assertEqual(app.state(), "withdrawn")
        self.assertTrue(tray.shown)
        self.assertIs(app.timer.state, State.RUNNING)
        self.assertIsNotNone(app._tick_job)
        app._tray = None

    def test_sound_switch_really_gates_completion_chime(self):
        app = self.app
        calls = []
        app._play_sound = lambda: calls.append("chime") if app.sound.get() else None

        app.sound.set(True)
        app._begin_alert()
        self.assertEqual(calls, ["chime"])

        app._stop_alert()
        app.sound.set(False)
        app._begin_alert()
        self.assertEqual(calls, ["chime"])

    def test_volume_slider_tracks_sound_switch_and_updates_label(self):
        app = self.app
        app.sound.set(False)
        app._sound_changed()
        self.assertEqual(app.volume_slider.cget("state"), "disabled")

        app.sound.set(True)
        app._sound_changed()
        self.assertEqual(app.volume_slider.cget("state"), "normal")

        app.volume.set(35)
        app._volume_changed()
        self.assertEqual(app.volume_label.cget("text"), "35%")

    def test_zero_volume_does_not_start_chime(self):
        app = self.app
        app.sound.set(True)
        app.volume.set(0)
        app._volume_changed()
        app._chime_thread = None
        app._play_sound()
        self.assertIsNone(app._chime_thread)


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.path = Path(self._temp.name) / "settings.json"
        self.app = None

    def tearDown(self):
        if self.app is not None:
            self.app._quit()
            self.app = None
        self._temp.cleanup()

    def test_saved_preferences_are_restored(self):
        app = self.app = module.TimerApp(
            enable_tray=False, settings_path=self.path,
        )
        app.volume.set(40)
        app._volume_changed(40)
        app.sound.set(False)
        app._sound_changed()
        app.pinned.set(True)
        app._pinned_changed()
        app._select_preset(300)
        app._quit()
        self.app = None
        self.assertTrue(self.path.exists())

        app = self.app = module.TimerApp(
            enable_tray=False, settings_path=self.path,
        )
        self.assertFalse(app.sound.get())
        self.assertEqual(app.volume.get(), 40)
        self.assertEqual(app.volume_label.cget("text"), "40%")
        self.assertTrue(app.pinned.get())
        self.assertEqual(app.timer.duration, 300)
        self.assertEqual([part.get() for part in app.parts], ["00", "05", "00"])
        self.assertEqual(app.volume_slider.cget("state"), "disabled")

    def test_invalid_settings_fall_back_to_defaults(self):
        self.path.write_text(
            '{"sound": "yes", "volume": 140, "pinned": 1, "duration": 0}',
            encoding="utf-8",
        )
        settings = module.load_settings(self.path)
        self.assertTrue(settings.sound)
        self.assertEqual(settings.volume, module.DEFAULT_VOLUME)
        self.assertFalse(settings.pinned)
        self.assertEqual(settings.duration, module.DEFAULT_DURATION)

    def test_missing_or_broken_file_uses_defaults(self):
        self.assertEqual(module.load_settings(self.path), module.Settings())
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(module.load_settings(self.path), module.Settings())
        self.assertEqual(module.load_settings(None), module.Settings())


class TrayTests(unittest.TestCase):
    def test_menu_is_callable_and_labels_follow_timer_state(self):
        class Timer:
            state = State.RUNNING

        class App:
            timer = Timer()

        tray = object.__new__(module.SystemTray)
        tray._app = App()
        items = tray._menu_items()
        self.assertEqual(items[1].text, "Пауза")
        self.assertTrue(items[1].enabled)
        App.timer.state = State.PAUSED
        self.assertEqual(tray._menu_items()[1].text, "Продолжить")
        App.timer.state = State.FINISHED
        self.assertFalse(tray._menu_items()[1].enabled)

    def test_icon_artwork_has_distinct_colors_for_each_state(self):
        from PIL import ImageChops

        active = module.SystemTray._image("#91DDBA")
        paused = module.SystemTray._image("#E7C787")
        self.assertEqual(active.size, (64, 64))
        self.assertIsNotNone(
            ImageChops.difference(active.convert("RGB"), paused.convert("RGB")).getbbox()
        )


if __name__ == "__main__":
    unittest.main()
