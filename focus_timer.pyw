"""FOCUS — a single-file desktop timer.

Run with pythonw.exe (or double-click this .pyw file), never python.exe.
Build: python -m PyInstaller --onefile --noconsole --collect-all customtkinter
       --collect-all pystray --name FocusTimer focus_timer.pyw
"""

from __future__ import annotations

import array
import ctypes
import io
import json
import logging
import math
import os
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
import wave
from tkinter import messagebox
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable

try:
    import customtkinter as ctk
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    if __name__ == "__main__":
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "FOCUS · Нужна библиотека",
            "Установите CustomTkinter в тот же Python, которым запускаете таймер:\n\n"
            "py -m pip install -r requirements.txt\n\n"
            "Затем откройте focus_timer.pyw через pythonw.exe.",
            parent=root,
        )
        root.destroy()
        raise SystemExit(1)
    raise

# winsound refuses SND_ASYNC together with SND_MEMORY, so the application
# calls Win32's PlaySoundW directly.  The WAV lives in memory while it plays.
if sys.platform == "win32":
    try:
        _WINMM_PLAY_SOUND = ctypes.WinDLL("winmm").PlaySoundW
        _WINMM_PLAY_SOUND.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint)
        _WINMM_PLAY_SOUND.restype = ctypes.c_bool
    except (AttributeError, OSError):
        _WINMM_PLAY_SOUND = None
else:
    _WINMM_PLAY_SOUND = None


class SystemTray:
    """Windows notification-area icon with state-colored artwork and a menu."""

    def __init__(self, app: "TimerApp") -> None:
        self._app = app
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._last_status: tuple[str, str] | None = None
        self._last_label: str | None = None
        self._lock = threading.RLock()
        self.icon = pystray.Icon(
            "focus_timer",
            icon=self._image(app.ACCENT),
            title="FOCUS · Таймер",
            # A callable menu is rebuilt by pystray in its own thread whenever
            # the menu opens, so labels stay current without cross-thread edits.
            menu=pystray.Menu(self._menu_items),
        )
        # pystray owns a small Windows message-loop thread. Keep the icon hidden
        # until the user minimizes the window; setup signals when the backend is
        # ready, without touching Tk or showing the icon from that thread.
        self._worker = threading.Thread(
            target=self._run, name="focus-system-tray", daemon=True,
        )
        self._worker.start()
        if not self._ready.wait(timeout=5):
            self.stop()
            raise RuntimeError("Не удалось запустить значок FOCUS в системном трее.")
        if self._error is not None:
            raise RuntimeError("Не удалось запустить значок FOCUS в системном трее.") from self._error

    def _run(self) -> None:
        try:
            self.icon.run(setup=self._setup)
        except BaseException as error:
            self._error = error
            logging.exception("FOCUS tray loop failed")
        finally:
            self._ready.set()

    @staticmethod
    def _image(color: str) -> Image.Image:
        """Bold clock glyph that stays legible when Windows shrinks it to 16 px."""
        scale = 4
        canvas = Image.new("RGBA", (64 * scale, 64 * scale), (0, 0, 0, 0))
        draw = ImageDraw.Draw(canvas)
        center = 32 * scale
        radius = 29 * scale
        rgb = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5)) + (255,)
        # Outline the colored clock in black and use black hands: the symbol
        # remains visible on both dark and light Windows taskbars.
        outline = (13, 15, 20, 255)
        outer = radius + 3 * scale
        draw.ellipse(
            (center - outer, center - outer, center + outer, center + outer),
            fill=outline,
        )
        draw.ellipse(
            (center - radius, center - radius, center + radius, center + radius),
            fill=rgb,
        )
        hand = outline
        draw.rounded_rectangle(
            (center - 3 * scale, center - 17 * scale, center + 3 * scale, center + 3 * scale),
            radius=3 * scale, fill=hand,
        )
        draw.rounded_rectangle(
            (center - 3 * scale, center - 3 * scale, center + 15 * scale, center + 3 * scale),
            radius=3 * scale, fill=hand,
        )
        return canvas.resize((64, 64), Image.Resampling.LANCZOS)

    def _setup(self, icon) -> None:
        self._ready.set()

    def _menu_items(self):
        state = self._app.timer.state
        toggle = {
            State.RUNNING: "Пауза",
            State.PAUSED: "Продолжить",
            State.READY: "Старт",
            State.FINISHED: "Ещё раз",
        }[state]
        return (
            pystray.MenuItem(
                "Открыть FOCUS",
                lambda icon, item: self._app._post_to_tk("restore"),
                default=True,
            ),
            pystray.MenuItem(
                toggle,
                lambda icon, item: self._app._post_to_tk("toggle"),
                enabled=state is not State.FINISHED,
            ),
            pystray.MenuItem(
                "Сбросить таймер",
                lambda icon, item: self._app._post_to_tk("reset"),
                enabled=state is not State.READY,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Выйти из FOCUS",
                lambda icon, item: self._app._post_to_tk("quit"),
            ),
        )

    def update(self, state: State, seconds: int, color: str) -> None:
        label = {
            State.READY: "Готов к старту",
            State.RUNNING: "Идёт отсчёт",
            State.PAUSED: "На паузе",
            State.FINISHED: "Время вышло",
        }[state]
        tooltip = f"FOCUS · {label} · {seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
        with self._lock:
            if self._last_status == (tooltip, color):
                return
            previous = self._last_status
            self._last_status = (tooltip, color)
            self._last_label = label
            try:
                self.icon.title = tooltip[:127]
                if previous is None or previous[1] != color:
                    self.icon.icon = self._image(color)
            except Exception:
                logging.exception("Could not update the FOCUS tray icon")

    def show(self) -> None:
        if self._ready.wait(timeout=5) and self._error is None:
            with self._lock:
                self.icon.visible = True

    def notify(self, message: str) -> None:
        """Balloon tip; best-effort because Windows may suppress notifications."""
        if self._ready.is_set() and self._error is None:
            try:
                with self._lock:
                    self.icon.notify(message, "FOCUS · Таймер")
            except Exception:
                logging.exception("Could not show the FOCUS tray notification")

    def hide(self) -> None:
        if self._ready.is_set() and self._error is None:
            with self._lock:
                if self.icon.visible:
                    self.icon.visible = False

    def stop(self) -> None:
        if self._ready.is_set() and self._error is None:
            try:
                with self._lock:
                    self.icon.stop()
            except Exception:
                logging.exception("Could not stop the FOCUS tray icon")
        worker = getattr(self, "_worker", None)
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=3)
            if worker.is_alive():
                logging.error("FOCUS tray thread did not stop within three seconds")


MAX_DURATION = 99 * 3600 + 59 * 60 + 59
DEFAULT_DURATION = 15 * 60
DEFAULT_VOLUME = 70
CHIME_SAMPLE_RATE = 22050
# A longer, gently decaying completion melody, synthesized in memory: no audio
# file needed. Zero-frequency entries are pauses; the whole signal lasts ~7 s.
CHIME_NOTES = (
    (659, 260), (880, 260), (1047, 480), (0, 300),
    (784, 260), (988, 260), (1319, 480), (0, 300),
    (659, 260), (880, 260), (1047, 260), (1319, 800), (0, 650),
    (1047, 320), (1319, 320), (1568, 500), (1047, 1300),
)


def build_chime_wav(volume: int = DEFAULT_VOLUME) -> bytes:
    """Render the completion melody into a complete 16-bit mono WAV file.

    Synthesizing our own waveform (instead of winsound.Beep) makes the volume
    adjustable and allows the chime to ring for several seconds.
    """
    level = max(0, min(100, int(volume))) / 100
    amplitude = 0.35 * level
    samples = array.array("h")
    attack_frames = max(1, int(CHIME_SAMPLE_RATE * 0.010))
    release_frames = max(1, int(CHIME_SAMPLE_RATE * 0.035))
    for frequency, milliseconds in CHIME_NOTES:
        frames = max(1, int(CHIME_SAMPLE_RATE * milliseconds / 1000))
        if frequency <= 0:
            samples.frombytes(b"\x00\x00" * frames)
            continue
        omega = math.tau * frequency / CHIME_SAMPLE_RATE
        for index in range(frames):
            envelope = min(1.0, index / attack_frames)
            if frames - index < release_frames:
                envelope *= (frames - index) / release_frames
            decay = math.exp(-2.2 * index / frames)
            phase = omega * index
            tone = (
                math.sin(phase)
                + 0.30 * math.sin(2 * phase)
                + 0.10 * math.sin(3 * phase)
            )
            samples.append(int(32767 * amplitude * envelope * decay * tone))
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(CHIME_SAMPLE_RATE)
        wav.writeframes(samples.tobytes())
    return output.getvalue()


@dataclass(frozen=True)
class Settings:
    """Validated preferences restored from the JSON settings file."""

    sound: bool = True
    volume: int = DEFAULT_VOLUME
    pinned: bool = False
    duration: int = DEFAULT_DURATION


def load_settings(path: Path | None) -> Settings:
    """Read settings defensively; malformed files fall back to defaults."""
    if path is None:
        return Settings()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Settings()
    if not isinstance(data, dict):
        return Settings()

    duration = data.get("duration", DEFAULT_DURATION)
    if (
        isinstance(duration, bool)
        or not isinstance(duration, int)
        or not 1 <= duration <= MAX_DURATION
    ):
        duration = DEFAULT_DURATION
    volume = data.get("volume", DEFAULT_VOLUME)
    if (
        isinstance(volume, bool)
        or not isinstance(volume, int)
        or not 0 <= volume <= 100
    ):
        volume = DEFAULT_VOLUME
    sound = data.get("sound", True)
    if not isinstance(sound, bool):
        sound = True
    pinned = data.get("pinned", False)
    if not isinstance(pinned, bool):
        pinned = False
    return Settings(sound=sound, volume=volume, pinned=pinned, duration=duration)


class State(Enum):
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    FINISHED = "finished"


@dataclass
class Countdown:
    """UI-independent state machine. A deadline prevents accumulated tick drift."""

    duration: int = DEFAULT_DURATION
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)
    remaining: float = field(init=False)
    state: State = field(default=State.READY, init=False)
    deadline: float | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.set_duration(self.duration)

    def set_duration(self, seconds: int) -> None:
        if self.state is State.RUNNING:
            raise RuntimeError("Pause the countdown before changing its duration.")
        if isinstance(seconds, bool) or not isinstance(seconds, int):
            raise ValueError("Duration must be an integer.")
        if not 0 <= seconds <= MAX_DURATION:
            raise ValueError("Duration must be between 0 and 99:59:59.")
        self.duration = seconds
        self.reset()

    def start(self) -> bool:
        if self.state is State.RUNNING or self.duration == 0:
            return False
        if self.state is State.FINISHED:
            self.remaining = float(self.duration)
        self.deadline = self.clock() + self.remaining
        self.state = State.RUNNING
        return True

    def update(self) -> bool:
        """Return True exactly once when the running timer expires."""
        if self.state is not State.RUNNING:
            return False
        assert self.deadline is not None
        self.remaining = max(0.0, self.deadline - self.clock())
        if self.remaining == 0:
            self.state = State.FINISHED
            self.deadline = None
            return True
        return False

    def pause(self) -> bool:
        if self.state is not State.RUNNING:
            return False
        if self.update():
            return True
        self.state = State.PAUSED
        self.deadline = None
        return False

    def reset(self) -> None:
        self.remaining = float(self.duration)
        self.deadline = None
        self.state = State.READY

    @property
    def display_seconds(self) -> int:
        # Keep "00:00:01" until the real deadline instead of finishing early.
        return math.ceil(self.remaining)

    @property
    def progress(self) -> float:
        if self.duration == 0:
            return 0.0
        return max(0.0, min(1.0, 1.0 - self.remaining / self.duration))


def split_time(seconds: int) -> tuple[int, int, int]:
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return hours, minutes, seconds


def valid_part(value: str, maximum: int) -> bool:
    """Allow empty fields while editing, but reject non-ASCII digits and overflow."""
    return value == "" or (
        len(value) <= 2 and value.isascii() and value.isdecimal()
        and int(value) <= maximum
    )


class TimerApp(ctk.CTk):
    BG = "#101116"
    SURFACE = "#191C25"
    INPUT = "#13161E"
    BORDER = "#2A2E3B"
    TEXT = "#F2F3FA"
    MUTED = "#9298AD"
    ACCENT = "#A5B3FF"
    ACCENT_HOVER = "#BAC5FF"
    ACCENT_SURFACE = "#29314D"
    SUCCESS = "#91DDBA"
    SUCCESS_SURFACE = "#203B32"

    def __init__(
        self, *, enable_tray: bool = False, settings_path: Path | None = None,
    ) -> None:
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        super().__init__(fg_color=self.BG)
        self.withdraw()
        self.title("FOCUS · Таймер")
        self.geometry("740x860")
        self.minsize(660, 820)
        self.resizable(True, True)
        self.protocol("WM_DELETE_WINDOW", self._close)

        settings = load_settings(settings_path)
        self._settings_path = settings_path
        self._settings_job: str | None = None
        self.timer = Countdown(settings.duration)
        self._closing = False
        self._setting_inputs = False
        self._tick_job: str | None = None
        self._alert_job: str | None = None
        self._last_render: tuple[int, State] | None = None
        self._tray: SystemTray | None = None
        self._tray_enabled = enable_tray
        self._tray_commands: queue.SimpleQueue[str] = queue.SimpleQueue()
        self._chime_generation = 0
        self._chime_thread: threading.Thread | None = None
        self._chime_lock = threading.Lock()
        self._chime_buffer: object | None = None
        self._chime_cache: dict[int, bytes] = {}
        self.sound = tk.BooleanVar(self, value=settings.sound)
        self.volume = tk.IntVar(self, value=settings.volume)
        self.pinned = tk.BooleanVar(self, value=settings.pinned)
        self.parts = [
            tk.StringVar(self, value=f"{value:02d}")
            for value in split_time(settings.duration)
        ]
        self._entries: list[ctk.CTkEntry] = []
        self._preset_buttons: dict[int, ctk.CTkButton] = {}
        self._time_labels: list[ctk.CTkLabel] = []
        self._font = ctk.CTkFont(family="Segoe UI", size=14)
        self._digit_font = ctk.CTkFont(family="Consolas", size=84, weight="bold")

        self._build_ui()
        self._create_icon()
        if self._tray_enabled:
            try:
                self._tray = SystemTray(self)
            except Exception:
                logging.exception("System tray unavailable; using the taskbar instead")
        for variable in self.parts:
            variable.trace_add("write", self._inputs_changed)
        self.bind("<space>", self._space_shortcut)
        self.bind("<Return>", self._enter_shortcut)
        self.bind("<KeyPress-r>", self._reset_shortcut)
        self.bind("<KeyPress-R>", self._reset_shortcut)
        self.bind("<Configure>", self._resize_digits)
        self._sync_controls()
        self._render()
        if self._tray is None:
            self.tray_button.configure(
                state="disabled", text="Трей недоступен",
                text_color_disabled=self.MUTED,
            )
        self.after(0, self._show_window)
        self._tick_job = self.after(50, self._tick)

    def _label(self, parent, text: str, **kwargs) -> ctk.CTkLabel:
        options = dict(text_color=self.TEXT, font=self._font)
        options.update(kwargs)
        return ctk.CTkLabel(parent, text=text, **options)

    def _button(self, parent, text: str, command, **kwargs) -> ctk.CTkButton:
        options = dict(
            height=44, corner_radius=12, border_width=1,
            border_color=self.BORDER, fg_color=self.SURFACE,
            hover_color="#262B39", text_color=self.TEXT,
            text_color_disabled="#63697D", font=self._font,
        )
        options.update(kwargs)
        return ctk.CTkButton(parent, text=text, command=command, **options)

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        shell = ctk.CTkFrame(self, fg_color="transparent")
        shell.grid(row=0, column=0, sticky="nsew", padx=32, pady=(26, 18))
        shell.grid_columnconfigure(0, weight=1)
        shell.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(shell, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 24))
        header.grid_columnconfigure(1, weight=1)
        mark = ctk.CTkFrame(
            header, width=46, height=46, corner_radius=14,
            fg_color=self.ACCENT_SURFACE,
        )
        mark.grid(row=0, column=0, rowspan=2, padx=(0, 13))
        mark.grid_propagate(False)
        self._label(
            mark, "◷", font=ctk.CTkFont(family="Segoe UI Symbol", size=30),
            text_color=self.ACCENT,
        ).place(relx=.5, rely=.5, anchor="center")
        self._label(
            header, "F O C U S", anchor="w",
            font=ctk.CTkFont(family="Segoe UI", size=21, weight="bold"),
        ).grid(row=0, column=1, sticky="ew")
        self._label(
            header, "Пространство для важного", anchor="w",
            text_color=self.MUTED, font=ctk.CTkFont(size=12),
        ).grid(row=1, column=1, sticky="ew")
        self.status = self._label(
            header, "●  Готов к старту", height=32, width=138,
            corner_radius=16, fg_color=self.ACCENT_SURFACE,
            text_color=self.ACCENT, font=ctk.CTkFont(size=12),
        )
        self.status.grid(row=0, column=2, rowspan=2, padx=(12, 0))

        self.card = ctk.CTkFrame(
            shell, fg_color=self.SURFACE, corner_radius=24,
            border_width=1, border_color=self.BORDER,
        )
        self.card.grid(row=1, column=0, sticky="nsew", pady=(0, 22))
        self.card.grid_columnconfigure(0, weight=1)
        self.card.grid_rowconfigure((1, 4), weight=1)
        self.eyebrow = self._label(
            self.card, "О С Т А Л О С Ь   В Р Е М Е Н И",
            text_color=self.MUTED, font=ctk.CTkFont(size=11), height=18,
        )
        self.eyebrow.grid(row=0, column=0, pady=(28, 6))
        digits = ctk.CTkFrame(self.card, fg_color="transparent")
        digits.grid(row=2, column=0, padx=18)
        for index, unit in enumerate(("ЧАСЫ", "МИНУТЫ", "СЕКУНДЫ")):
            column = index * 2
            label = self._label(digits, "00", font=self._digit_font)
            label.grid(row=0, column=column, padx=2)
            self._time_labels.append(label)
            self._label(
                digits, unit, text_color=self.MUTED,
                font=ctk.CTkFont(size=10), height=18,
            ).grid(row=1, column=column, pady=(0, 4))
            if index < 2:
                self._label(
                    digits, ":", font=self._digit_font, text_color="#555C72",
                ).grid(row=0, column=column + 1, padx=2)
        self.detail = self._label(
            self.card, "Ваш следующий фокус-сеанс · 15 мин",
            text_color=self.MUTED, font=ctk.CTkFont(size=13), height=20,
        )
        self.detail.grid(row=3, column=0, pady=(15, 0))
        progress_row = ctk.CTkFrame(self.card, fg_color="transparent")
        progress_row.grid(row=5, column=0, sticky="ew", padx=28, pady=(12, 24))
        progress_row.grid_columnconfigure(0, weight=1)
        self.progress = ctk.CTkProgressBar(
            progress_row, height=5, corner_radius=3, fg_color=self.BORDER,
            progress_color=self.ACCENT,
        )
        self.progress.grid(row=0, column=0, sticky="ew", padx=(0, 14))
        self.percent = self._label(
            progress_row, "0%", width=36, text_color=self.MUTED,
            font=ctk.CTkFont(size=11), anchor="e", height=16,
        )
        self.percent.grid(row=0, column=1)

        presets = ctk.CTkFrame(shell, fg_color="transparent")
        presets.grid(row=2, column=0, sticky="ew", pady=(0, 20))
        presets.grid_columnconfigure((0, 1, 2, 3), weight=1, uniform="preset")
        self._label(
            presets, "БЫСТРЫЙ ВЫБОР", text_color=self.MUTED, anchor="w",
            font=ctk.CTkFont(size=11),
        ).grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, 10))
        for index, (seconds, text) in enumerate(
            ((300, "5 мин"), (900, "15 мин"), (1800, "30 мин"), (3600, "1 час"))
        ):
            button = self._button(
                presets, text, lambda s=seconds: self._select_preset(s),
                width=1, height=40,
            )
            button.grid(
                row=1, column=index, sticky="ew",
                padx=(0 if index == 0 else 5, 0 if index == 3 else 5),
            )
            self._preset_buttons[seconds] = button

        inputs = ctk.CTkFrame(shell, fg_color="transparent")
        inputs.grid(row=3, column=0, sticky="ew", pady=(0, 6))
        inputs.grid_columnconfigure((0, 1, 2), weight=1, uniform="input")
        for index, (unit, maximum) in enumerate(
            (("Часы · 0–99", 99), ("Минуты · 0–59", 59), ("Секунды · 0–59", 59))
        ):
            group = ctk.CTkFrame(inputs, fg_color="transparent")
            group.grid(
                row=0, column=index, sticky="ew",
                padx=(0 if index == 0 else 6, 0 if index == 2 else 6),
            )
            group.grid_columnconfigure(0, weight=1)
            self._label(
                group, unit, text_color=self.MUTED, anchor="w",
                font=ctk.CTkFont(size=12),
            ).grid(row=0, column=0, sticky="ew", pady=(0, 8))
            validator = self.register(lambda value, m=maximum: valid_part(value, m))
            entry = ctk.CTkEntry(
                group, textvariable=self.parts[index], height=48,
                corner_radius=12, border_width=1, border_color=self.BORDER,
                fg_color=self.INPUT, text_color=self.TEXT, justify="center",
                font=ctk.CTkFont(family="Consolas", size=23),
                validate="key", validatecommand=(validator, "%P"),
            )
            entry.grid(row=1, column=0, sticky="ew")
            entry.bind("<FocusOut>", lambda event, i=index: self._normalize_part(i))
            entry.bind("<Up>", lambda event, i=index: self._step_part(i, 1))
            entry.bind("<Down>", lambda event, i=index: self._step_part(i, -1))
            self._entries.append(entry)

        self.hint = self._label(
            shell, "Настройте время или выберите готовый пресет",
            text_color=self.MUTED, anchor="w", font=ctk.CTkFont(size=11),
        )
        self.hint.grid(row=4, column=0, sticky="ew", pady=(3, 15))
        controls = ctk.CTkFrame(shell, fg_color="transparent")
        controls.grid(row=5, column=0, sticky="ew", pady=(0, 20))
        controls.grid_columnconfigure(0, weight=2, uniform="control")
        controls.grid_columnconfigure((1, 2), weight=1, uniform="control")
        self.start_button = self._button(
            controls, "▶  Старт", self._start, width=1, height=52,
            fg_color=self.ACCENT, hover_color=self.ACCENT_HOVER,
            border_width=0, text_color=self.BG,
            font=ctk.CTkFont(family="Segoe UI", size=15, weight="bold"),
        )
        self.start_button.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.pause_button = self._button(
            controls, "Ⅱ  Пауза", self._pause, width=1, height=52,
        )
        self.pause_button.grid(row=0, column=1, sticky="ew", padx=(0, 10))
        self.reset_button = self._button(
            controls, "↺  Сброс", self._reset, width=1, height=52,
        )
        self.reset_button.grid(row=0, column=2, sticky="ew")

        options = ctk.CTkFrame(shell, fg_color="transparent")
        options.grid(row=6, column=0, sticky="ew")
        options.grid_columnconfigure(0, weight=1)
        options.grid_columnconfigure(1, weight=0)
        options.grid_columnconfigure(2, weight=1)
        switch_options = dict(
            font=ctk.CTkFont(size=12), text_color=self.MUTED,
            progress_color=self.ACCENT, fg_color=self.BORDER,
            button_color=self.TEXT, button_hover_color="#FFFFFF",
            switch_width=34, switch_height=18,
        )
        ctk.CTkSwitch(
            options, text="Звук в конце", variable=self.sound,
            command=self._sound_changed, **switch_options,
        ).grid(row=0, column=0, sticky="w")
        self.tray_button = self._button(
            options, "↓  В трей", self._minimize_to_tray,
            width=104, height=34, corner_radius=10,
            font=ctk.CTkFont(size=12),
        )
        self.tray_button.grid(row=0, column=1, padx=12)
        ctk.CTkSwitch(
            options, text="Поверх окон", variable=self.pinned,
            command=self._pinned_changed,
            **switch_options,
        ).grid(row=0, column=2, sticky="e")

        volume_row = ctk.CTkFrame(shell, fg_color="transparent")
        volume_row.grid(row=7, column=0, sticky="ew", pady=(12, 0))
        volume_row.grid_columnconfigure(1, weight=1)
        self._label(
            volume_row, "Громкость сигнала", text_color=self.MUTED,
            font=ctk.CTkFont(size=12),
        ).grid(row=0, column=0, sticky="w")
        self.volume_slider = ctk.CTkSlider(
            volume_row, from_=0, to=100, number_of_steps=20,
            variable=self.volume, command=self._volume_changed,
            state="normal" if self.sound.get() else "disabled",
            height=16, corner_radius=8, border_width=0,
            fg_color=self.BORDER, progress_color=self.ACCENT,
            button_color=self.TEXT, button_hover_color="#FFFFFF",
        )
        self.volume_slider.grid(row=0, column=1, sticky="ew", padx=(16, 14))
        self.volume_label = self._label(
            volume_row, f"{self._volume_percent()}%", width=42, anchor="e",
            text_color=self.MUTED, font=ctk.CTkFont(family="Consolas", size=12),
        )
        self.volume_label.grid(row=0, column=2, sticky="e")
        self._sync_sound_controls()
        self._label(
            shell, "Пробел — старт / пауза     ·     R — сброс",
            text_color="#646B80", font=ctk.CTkFont(size=11),
        ).grid(row=8, column=0, pady=(18, 0))

    def _create_icon(self) -> None:
        """A code-drawn icon: no PNG, ICO or external assets needed at runtime."""
        self._icon = tk.PhotoImage(master=self, width=64, height=64)
        rows = []
        for y in range(64):
            row = []
            for x in range(64):
                radius = math.hypot(x - 31.5, y - 31.5)
                ring = 19 <= radius <= 22
                hands = (
                    (30 <= x <= 33 and 17 <= y <= 33)
                    or (30 <= y <= 33 and 31 <= x <= 44)
                )
                row.append(self.ACCENT if ring or hands else self.BG)
            rows.append("{" + " ".join(row) + "}")
        self._icon.put(" ".join(rows))
        self.iconphoto(True, self._icon)

    def _show_window(self) -> None:
        self.update_idletasks()
        width, height = self.winfo_width(), self.winfo_height()
        x = max(0, (self.winfo_screenwidth() - width) // 2)
        y = max(0, (self.winfo_screenheight() - height) // 2 - 24)
        self.geometry(f"+{x}+{y}")
        self.attributes("-topmost", self.pinned.get())
        self.deiconify()
        if sys.platform == "win32":
            try:
                hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
                dark = ctypes.c_int(1)
                for attribute in (20, 19):
                    result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        hwnd, attribute, ctypes.byref(dark), ctypes.sizeof(dark)
                    )
                    if result == 0:
                        break
            except (AttributeError, OSError):
                pass  # Standard native title bar is a safe fallback.

    def _resize_digits(self, event) -> None:
        if event.widget is self:
            # CTk roots track window scaling, not widget scaling.
            logical_width = event.width / self._get_window_scaling()
            size = min(96, max(66, int((logical_width - 190) / 6.1)))
            if self._digit_font.cget("size") != size:
                self._digit_font.configure(size=size)

    def _read_inputs(self) -> int:
        values = []
        for variable, maximum in zip(self.parts, (99, 59, 59)):
            text = variable.get()
            if not valid_part(text, maximum):
                raise ValueError("Проверьте время: часы 0–99, минуты и секунды 0–59.")
            values.append(int(text or "0"))
        return values[0] * 3600 + values[1] * 60 + values[2]

    def _inputs_changed(self, *_args) -> None:
        if self._setting_inputs or self.timer.state is State.RUNNING:
            return
        try:
            seconds = self._read_inputs()
        except ValueError as error:
            self.hint.configure(text=str(error), text_color=self.ACCENT)
            return
        self._stop_alert()
        self.timer.set_duration(seconds)
        self._sync_controls()
        self._render()
        self._schedule_settings_save()

    def _normalize_part(self, index: int) -> None:
        if self.timer.state is State.RUNNING:
            return
        # Padding must not reset a paused timer: suppress the StringVar trace.
        self._setting_inputs = True
        try:
            self.parts[index].set(f"{int(self.parts[index].get() or '0'):02d}")
        finally:
            self._setting_inputs = False

    def _step_part(self, index: int, direction: int) -> str:
        if self.timer.state is not State.RUNNING:
            value = int(self.parts[index].get() or "0")
            value = max(0, min((99, 59, 59)[index], value + direction))
            self.parts[index].set(f"{value:02d}")
        return "break"

    def _select_preset(self, seconds: int) -> None:
        if self.timer.state is State.RUNNING:
            return
        self._stop_alert()
        self._setting_inputs = True
        try:
            for variable, value in zip(self.parts, split_time(seconds)):
                variable.set(f"{value:02d}")
        finally:
            self._setting_inputs = False
        self.timer.set_duration(seconds)
        self._sync_controls()
        self._render()
        self._schedule_settings_save()
        self.focus_set()

    def _start(self) -> None:
        if self.timer.state is State.RUNNING:
            return
        try:
            seconds = self._read_inputs()
        except ValueError as error:
            self.hint.configure(text=str(error), text_color=self.ACCENT)
            return
        if seconds == 0:
            self.hint.configure(
                text="Укажите время больше нуля, чтобы запустить таймер.",
                text_color=self.ACCENT,
            )
            return
        self._stop_alert()
        for index in range(3):
            self._normalize_part(index)
        # Resume preserves fractional seconds; a finished timer restarts itself.
        if self.timer.state is State.READY:
            self.timer.set_duration(seconds)
        self.timer.start()
        self.focus_set()
        self._sync_controls()
        self._render()
        self._schedule_settings_save()

    def _pause(self) -> None:
        if self.timer.state is not State.RUNNING:
            return
        finished = self.timer.pause()
        self._sync_controls()
        self._render()
        if finished:
            self._begin_alert()

    def _reset(self) -> None:
        self._stop_alert()
        self.timer.reset()
        self._sync_controls()
        self._render()
        self.focus_set()

    def _sync_controls(self) -> None:
        running = self.timer.state is State.RUNNING
        state = "disabled" if running else "normal"
        for entry in self._entries:
            entry.configure(state=state)
        for seconds, button in self._preset_buttons.items():
            selected = seconds == self.timer.duration
            button.configure(
                state=state,
                fg_color=self.ACCENT_SURFACE if selected else self.SURFACE,
                border_color=self.ACCENT if selected else self.BORDER,
                text_color=self.ACCENT if selected else self.TEXT,
            )
        start_text = {
            State.READY: "▶  Старт",
            State.RUNNING: "Идёт отсчёт",
            State.PAUSED: "▶  Продолжить",
            State.FINISHED: "▶  Ещё раз",
        }[self.timer.state]
        self.start_button.configure(
            text=start_text,
            state="disabled" if running or self.timer.duration == 0 else "normal",
            fg_color=self.ACCENT_SURFACE if running else self.ACCENT,
        )
        self.pause_button.configure(state="normal" if running else "disabled")
        self.reset_button.configure(
            state="disabled" if self.timer.state is State.READY else "normal",
        )
        if running:
            hint = "Время зафиксировано. Для изменения сначала нажмите «Пауза»."
        elif self.timer.state is State.PAUSED:
            hint = "Пауза. Изменение времени или пресета начнёт новый отсчёт."
        elif self.timer.duration == 0:
            hint = "Укажите время больше нуля, чтобы запустить таймер."
        else:
            hint = "Настройте время или выберите готовый пресет · ↑ ↓ меняют значение"
        self.hint.configure(text=hint, text_color=self.MUTED)
        self._last_render = None

    def _render(self) -> None:
        self.progress.set(self.timer.progress)
        self.percent.configure(text=f"{min(100, int(self.timer.progress * 100))}%")
        key = (self.timer.display_seconds, self.timer.state)
        if key == self._last_render:
            return
        self._last_render = key
        for label, value in zip(self._time_labels, split_time(key[0])):
            label.configure(
                text=f"{value:02d}",
                text_color=self.SUCCESS if key[1] is State.FINISHED else self.TEXT,
            )
        text, color, background = {
            State.READY: ("●  Готов к старту", self.ACCENT, self.ACCENT_SURFACE),
            State.RUNNING: ("●  В фокусе", self.ACCENT, self.ACCENT_SURFACE),
            State.PAUSED: ("Ⅱ  На паузе", "#E7C787", "#3B3325"),
            State.FINISHED: ("✓  Завершён", self.SUCCESS, self.SUCCESS_SURFACE),
        }[key[1]]
        self.status.configure(text=text, text_color=color, fg_color=background)
        self.eyebrow.configure(
            text="В Р Е М Я   В Ы Ш Л О" if key[1] is State.FINISHED
            else "О С Т А Л О С Ь   В Р Е М Е Н И",
            text_color=self.SUCCESS if key[1] is State.FINISHED else self.MUTED,
        )
        self.progress.configure(
            progress_color=self.SUCCESS if key[1] is State.FINISHED else self.ACCENT,
        )
        if key[1] is State.RUNNING:
            end = datetime.now() + timedelta(seconds=self.timer.remaining)
            detail = f"Завершится в {end:%H:%M:%S}"
            if end.date() != datetime.now().date():
                detail += f" · {end:%d.%m}"
            self.title(f"{key[0] // 3600:02d}:{key[0] // 60 % 60:02d}:{key[0] % 60:02d} · FOCUS")
        elif key[1] is State.PAUSED:
            detail = "Можно выдохнуть. Продолжите, когда будете готовы."
            self.title("На паузе · FOCUS")
        elif key[1] is State.FINISHED:
            detail = "Отличная работа. Время сделать перерыв."
            self.title("Время вышло · FOCUS")
        else:
            h, m, s = split_time(self.timer.duration)
            duration = " ".join(
                f"{value} {unit}" for value, unit in ((h, "ч"), (m, "мин"), (s, "с"))
                if value
            ) or "0 с"
            detail = f"Ваш следующий фокус-сеанс · {duration}"
            self.title("FOCUS · Таймер")
        self.detail.configure(text=detail)

    def _tick(self) -> None:
        self._tick_job = None
        if self._closing:
            return
        self._process_tray_commands()
        if self.timer.update():
            self._sync_controls()
            self._render()
            self._begin_alert()
        else:
            self._render()
        self._update_tray()
        self._tick_job = self.after(50, self._tick)

    def _tray_color(self) -> str:
        return {
            State.READY: self.ACCENT,
            State.RUNNING: self.SUCCESS,
            State.PAUSED: "#E7C787",
            State.FINISHED: "#F08F9B",
        }[self.timer.state]

    def _update_tray(self) -> None:
        if self._tray is not None:
            self._tray.update(
                self.timer.state, self.timer.display_seconds, self._tray_color(),
            )

    def _post_to_tk(self, command: str) -> None:
        """Thread-safe handoff: pystray callbacks must never call Tk APIs."""
        if command in {"restore", "toggle", "reset", "quit"}:
            self._tray_commands.put(command)

    def _process_tray_commands(self) -> None:
        """Run queued tray actions on Tk's own event-loop thread."""
        actions = {
            "restore": self._restore_from_tray,
            "toggle": self._toggle_from_tray,
            "reset": self._reset_from_tray,
            "quit": self._quit,
        }
        while True:
            try:
                command = self._tray_commands.get_nowait()
            except queue.Empty:
                break
            if self._closing:
                break
            actions[command]()

    def _minimize_to_tray(self) -> None:
        if self._tray is None:
            self.iconify()
            self.hint.configure(
                text="Трей недоступен — окно свёрнуто на панель задач.",
                text_color=self.MUTED,
            )
            return
        self._update_tray()
        try:
            self._tray.show()
        except Exception:
            logging.exception("Could not show the FOCUS tray icon")
            self.iconify()
            return
        if self.timer.state is State.RUNNING:
            self._tray.notify(
                f"Таймер продолжает работу в фоне. Осталось "
                f"{self.timer.display_seconds // 60:02d}:"
                f"{self.timer.display_seconds % 60:02d}."
            )
        self.withdraw()

    def _restore_from_tray(self) -> None:
        if self._closing:
            return
        self.deiconify()
        self.state("normal")
        self.lift()
        self.focus_force()
        if self._tray is not None:
            try:
                self._tray.hide()
            except Exception:
                logging.exception("Could not hide the FOCUS tray icon")

    def _toggle_from_tray(self) -> None:
        if self.timer.state is State.RUNNING:
            self._pause()
        elif self.timer.state in (State.PAUSED, State.READY):
            self._restore_from_tray()
            self._start()

    def _reset_from_tray(self) -> None:
        self._reset()
        self._restore_from_tray()

    def _begin_alert(self) -> None:
        self._stop_alert()
        self._alert_started = time.monotonic()
        # Restore a minimized window, without stealing keyboard focus.
        if self.state() in ("iconic", "withdrawn"):
            self._restore_from_tray()
        if sys.platform == "win32":
            try:
                hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
                ctypes.windll.user32.FlashWindow(hwnd, True)
            except (AttributeError, OSError):
                pass
        if self.sound.get():
            self._play_sound()
        self._update_tray()
        self._animate_alert()

    def _animate_alert(self) -> None:
        self._alert_job = None
        if self._closing or self.timer.state is not State.FINISHED:
            return
        elapsed = time.monotonic() - self._alert_started
        if elapsed >= 3.0:
            self.card.configure(border_color=self.BORDER)
            return
        strength = (math.sin(elapsed * math.tau / 1.1 - math.pi / 2) + 1) / 2
        start = tuple(int(self.BORDER[i:i + 2], 16) for i in (1, 3, 5))
        end = tuple(int(self.SUCCESS[i:i + 2], 16) for i in (1, 3, 5))
        color = "#" + "".join(
            f"{round(a + (b - a) * strength):02x}" for a, b in zip(start, end)
        )
        self.card.configure(border_color=color)
        self._alert_job = self.after(40, self._animate_alert)

    def _schedule_settings_save(self) -> None:
        if self._settings_path is None or self._closing:
            return
        if self._settings_job is not None:
            self.after_cancel(self._settings_job)
        self._settings_job = self.after(400, self._save_settings)

    def _save_settings(self) -> None:
        self._settings_job = None
        path = self._settings_path
        if path is None:
            return
        data = {
            "sound": bool(self.sound.get()),
            "volume": self._volume_percent(),
            "pinned": bool(self.pinned.get()),
        }
        if self.timer.duration > 0:
            data["duration"] = self.timer.duration
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, path)
        except OSError:
            logging.exception("Could not save FOCUS settings")

    def _volume_percent(self) -> int:
        try:
            return max(0, min(100, int(round(self.volume.get()))))
        except (tk.TclError, TypeError, ValueError):
            return DEFAULT_VOLUME

    def _volume_changed(self, _value=None) -> None:
        percent = self._volume_percent()
        self.volume_label.configure(text=f"{percent}%")
        if percent == 0:
            self._silence()
        self._schedule_settings_save()

    def _pinned_changed(self) -> None:
        self.attributes("-topmost", self.pinned.get())
        self._schedule_settings_save()

    def _play_sound(self) -> None:
        """Play the completion melody once, unless muted or at zero volume."""
        volume = self._volume_percent()
        if not self.sound.get() or volume == 0:
            return
        if sys.platform != "win32":
            try:
                self.bell()
            except tk.TclError:
                pass
            return
        with self._chime_lock:
            if self._chime_thread is not None and self._chime_thread.is_alive():
                return  # Never stack chimes on top of each other.
            generation = self._chime_generation
            self._chime_thread = threading.Thread(
                target=self._chime_worker,
                args=(generation, volume),
                name="focus-chime",
                daemon=True,
            )
            thread = self._chime_thread
        thread.start()

    def _chime_bytes(self, volume: int) -> bytes:
        data = self._chime_cache.get(volume)
        if data is None:
            data = build_chime_wav(volume)
            if len(self._chime_cache) >= 8:
                self._chime_cache.clear()
            self._chime_cache[volume] = data
        return data

    def _chime_worker(self, generation: int, volume: int) -> None:
        try:
            import winsound
            data = self._chime_bytes(volume)
            # Win32 keeps reading this buffer while the sound plays; the app
            # owns a reference until playback is stopped.
            buffer = ctypes.create_string_buffer(data, len(data))
            with self._chime_lock:
                if generation != self._chime_generation or self._closing:
                    return  # Muted, reset, or quit while the melody was prepared.
                if _WINMM_PLAY_SOUND is None:
                    raise RuntimeError("Win32 PlaySoundW is unavailable")
                played = _WINMM_PLAY_SOUND(
                    buffer,
                    None,
                    winsound.SND_MEMORY | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
                )
                if not played:
                    self._chime_buffer = None
                    raise RuntimeError("Win32 PlaySoundW failed")
                # Keep the new buffer alive. Assign only after PlaySoundW has
                # taken over, so the previous sound never loses its memory early.
                self._chime_buffer = buffer
        except Exception:
            logging.debug("Melody chime unavailable", exc_info=True)
            try:
                import winsound
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
            except Exception:
                logging.debug("System chime unavailable", exc_info=True)

    def _sync_sound_controls(self) -> None:
        enabled = self.sound.get()
        self.volume_slider.configure(
            state="normal" if enabled else "disabled",
            progress_color=self.ACCENT if enabled else "#4A526B",
            fg_color=self.BORDER if enabled else "#242833",
            button_color=self.TEXT if enabled else "#6B7285",
        )

    def _sound_changed(self) -> None:
        self._sync_sound_controls()
        if not self.sound.get():
            self._silence()
        self._schedule_settings_save()

    def _silence(self) -> None:
        with self._chime_lock:
            self._chime_generation += 1
            if self._chime_buffer is not None:
                if _WINMM_PLAY_SOUND is not None:
                    try:
                        _WINMM_PLAY_SOUND(None, None, 0)
                    except OSError:
                        pass
                self._chime_buffer = None

    def _stop_alert(self) -> None:
        if self._alert_job is not None:
            self.after_cancel(self._alert_job)
            self._alert_job = None
        self._silence()
        self.card.configure(border_color=self.BORDER)

    @staticmethod
    def _is_editing(event) -> bool:
        return event.widget.winfo_class() in ("Entry", "TEntry", "Text")

    def _space_shortcut(self, event) -> str | None:
        if self._is_editing(event):
            return None
        if self.timer.state is State.RUNNING:
            self._pause()
        else:
            self._start()
        return "break"

    def _enter_shortcut(self, _event) -> str:
        if self.timer.state is State.RUNNING:
            self._pause()
        else:
            self._start()
        return "break"

    def _reset_shortcut(self, event) -> str | None:
        if self._is_editing(event):
            return None
        self._reset()
        return "break"

    def _close(self) -> None:
        # An active session keeps running in the background instead of dying
        # with the window, which is the whole point of the tray icon.
        if self._tray is not None and self.timer.state in (State.RUNNING, State.PAUSED):
            self._minimize_to_tray()
            return
        if self._tray is not None:
            should_hide = messagebox.askyesno(
                "FOCUS · Таймер",
                "Свернуть приложение в системный трей?\n\n"
                "«Да» — оставить FOCUS в области уведомлений.\n"
                "«Нет» — полностью выйти из приложения.",
                parent=self,
            )
            if should_hide:
                self._minimize_to_tray()
                return
        elif self.timer.state in (State.RUNNING, State.PAUSED) and messagebox.askyesno(
            "FOCUS · Таймер",
            "Свернуть окно на панель задач?\n\n"
            "«Нет» — выйти из приложения и остановить таймер.",
            parent=self,
        ):
            self.iconify()
            return
        self._quit()

    def _quit(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._stop_alert()
        if self._settings_job is not None:
            self.after_cancel(self._settings_job)
            self._settings_job = None
        self._save_settings()
        if self._tick_job is not None:
            self.after_cancel(self._tick_job)
            self._tick_job = None
        if self._tray is not None:
            tray, self._tray = self._tray, None
            tray.stop()
        # CTk also schedules DPI/title-bar callbacks. Cancel pending callbacks in
        # this root's interpreter so none can target widgets after destruction.
        for job in self.tk.call("after", "info"):
            self.after_cancel(job)
        self.destroy()

    def report_callback_exception(self, exc_type, value, traceback) -> None:
        logging.error("UI callback failed", exc_info=(exc_type, value, traceback))
        messagebox.showerror(
            "FOCUS · Ошибка",
            "Не удалось выполнить действие. Подробности записаны в focus_timer.log.",
            parent=self,
        )


def main() -> None:
    # No subprocesses, console allocation, shell commands or blocking sound loops.
    log_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "FocusTimer"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            filename=log_dir / "focus_timer.log", level=logging.ERROR,
            format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8",
        )
    except OSError:
        logging.basicConfig(handlers=[logging.NullHandler()])
    if sys.platform == "win32":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Focus.Timer")
        except (AttributeError, OSError):
            pass
    app = None
    try:
        app = TimerApp(
            enable_tray=True, settings_path=log_dir / "settings.json",
        )
        app.mainloop()
    except Exception:
        logging.exception("Application failed")
        error_root = app
        if error_root is None:
            error_root = tk.Tk()
            error_root.withdraw()
        messagebox.showerror(
            "FOCUS · Ошибка запуска",
            f"Не удалось открыть таймер.\nПодробности: {log_dir / 'focus_timer.log'}",
            parent=error_root,
        )
        error_root.destroy()


if __name__ == "__main__":
    main()
