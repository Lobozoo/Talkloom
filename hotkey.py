"""
hotkey.py — global hold-to-talk hotkey using pynput.

We deliberately use keyboard.Listener (raw on_press / on_release) rather than
GlobalHotKeys, because GlobalHotKeys only fires an "activated" callback and
gives no key-up event — and we need key-up to stop recording.

A hotkey is stored as a set of normalised key names, e.g. {"ctrl","alt","space"}.
The combo is considered *pressed* the moment every key in the set is down, and
*released* the moment any one of them goes up.

No admin rights are required for pynput global hooks on Windows.
"""

import logging
import sys

from pynput import keyboard

# Left/right modifier variants collapse to one canonical name so that
# e.g. right-Ctrl works the same as left-Ctrl.
_MOD_CANON = {
    "ctrl_l": "ctrl", "ctrl_r": "ctrl",
    "alt_l": "alt", "alt_r": "alt", "alt_gr": "alt",
    "shift_l": "shift", "shift_r": "shift",
    "cmd": "win", "cmd_l": "win", "cmd_r": "win",
}


# Windows virtual-key codes for the modifier names above — used to ask the OS
# whether a key we believe is held is REALLY still down.
_VK_FOR_NAME = {
    "ctrl": (0x11,), "alt": (0x12,), "shift": (0x10,), "win": (0x5B, 0x5C),
}


def _physically_down(name: str) -> bool | None:
    """Ask Windows whether a key is down right now. None = can't tell
    (non-Windows, or a key we have no virtual-key code for)."""
    if sys.platform != "win32":
        return None
    vks = _VK_FOR_NAME.get(name)
    if vks is None:
        if len(name) == 1 and name.isascii() and name.isalnum():
            vks = (ord(name.upper()),)      # letters/digits: VK == ASCII code
        else:
            return None
    try:
        import ctypes
        get = ctypes.windll.user32.GetAsyncKeyState
        return any(get(vk) & 0x8000 for vk in vks)
    except Exception:
        return None


def _key_name(listener: keyboard.Listener, key) -> str | None:
    """Normalise a pynput key event to a stable lowercase name."""
    # Named special keys (space, esc, f5, modifiers…) must be read from the
    # RAW event: on Windows, listener.canonical() strips them down to bare
    # virtual-key codes (Key.space -> KeyCode(vk=32)), losing the name.
    if isinstance(key, keyboard.Key):
        return _MOD_CANON.get(key.name, key.name)      # 'space', 'f5', 'ctrl'…

    # Character keys: canonical() strips modifier state — with Ctrl held, the
    # 'a' key would otherwise arrive as the control character '\x01'.
    try:
        key = listener.canonical(key)
    except Exception:
        pass

    if isinstance(key, keyboard.Key):                   # canonical may promote
        return _MOD_CANON.get(key.name, key.name)
    if isinstance(key, keyboard.KeyCode):
        if key.char:
            return key.char.lower()
        if key.vk is not None:
            return f"vk{key.vk}"                        # numpad / odd keys
    return None


class HotkeyListener:
    """Hold-to-talk: on_activate fires on combo key-down, on_deactivate on key-up."""

    def __init__(self, combo: list[str], on_activate, on_deactivate):
        self.combo = set(combo)
        self.on_activate = on_activate
        self.on_deactivate = on_deactivate
        # Secondary combo: TAP semantics (fires once on key-down, nothing on
        # key-up) — used for "add selected word to dictionary".
        self.secondary: set[str] = set()
        self.on_secondary = None
        self._sec_active = False
        self._pressed: set[str] = set()
        self._active = False
        self.paused = False   # set True while the settings dialog captures a new hotkey
        self._listener = keyboard.Listener(
            on_press=self._on_press, on_release=self._on_release
        )

    def set_secondary(self, combo: list[str], callback):
        """Register/replace the tap hotkey. Live — no restart needed."""
        self.secondary = set(combo or [])
        self.on_secondary = callback
        self._sec_active = False
        if combo:
            logging.info("Add-word hotkey registered: %s", "+".join(combo))

    # -- lifecycle ------------------------------------------------------

    def start(self):
        self._listener.start()

    def stop(self):
        self._listener.stop()

    def set_combo(self, combo: list[str]):
        """Swap the binding live — takes effect immediately, no restart needed."""
        self.combo = set(combo)
        self._pressed.clear()
        self._active = False
        logging.info("Hotkey re-registered: %s", "+".join(combo))

    def all_released(self) -> bool:
        """True when no tracked keys are physically down — used to delay text
        injection until the user's fingers are off the hotkey."""
        self._prune_stuck()
        return not self._pressed

    def _prune_stuck(self):
        """Forget keys we think are held but Windows says are up.

        A key-up can be missed entirely — the PC is locked with Win+L, a UAC
        prompt takes over, the hook is restarted mid-press. The key then
        stays in _pressed forever: every paste waits the full 2 s for
        "release", and the hotkey can fire with only some of its keys
        pressed. Iterates over a copy because the hook thread mutates the
        set concurrently."""
        for name in list(self._pressed):
            if _physically_down(name) is False:
                self._pressed.discard(name)

    def reconcile(self):
        """Called periodically from the GUI thread. If a missed key-up left
        the hold-to-talk combo 'active', end it — otherwise the microphone
        would stay open until the next press."""
        self._prune_stuck()
        if self._active and not (self.combo <= self._pressed):
            self._active = False
            logging.warning("Hotkey release was missed — ending recording")
            try:
                self.on_deactivate()
            except Exception:
                logging.exception("Hotkey on_deactivate failed")
        if self._sec_active and not (self.secondary <= self._pressed):
            self._sec_active = False

    def is_running(self) -> bool:
        return self._listener.running

    def restart(self):
        """Windows silently removes low-level hooks whose callbacks are slow,
        and hooks can also die across sleep/lock. A pynput Listener can't be
        restarted once stopped, so build a fresh one."""
        try:
            self._listener.stop()
        except Exception:
            pass
        self._pressed.clear()
        self._active = False
        self._listener = keyboard.Listener(
            on_press=self._on_press, on_release=self._on_release
        )
        self._listener.start()
        logging.warning("Hotkey listener restarted")

    # -- pynput callbacks (run on the listener thread) -------------------

    def _on_press(self, key):
        name = _key_name(self._listener, key)
        if name is None or self.paused:
            return
        # Prune BEFORE adding: inside a low-level hook, Windows has not yet
        # updated its key state for the key being pressed right now, so it
        # would look "up" and be pruned straight away.
        if name not in self._pressed:      # skip on auto-repeat
            self._prune_stuck()
        self._pressed.add(name)
        # OS auto-repeat re-fires on_press while held; _active guards against
        # starting the recorder more than once.
        if not self._active and self.combo and self.combo <= self._pressed:
            self._active = True
            try:
                self.on_activate()
            except Exception:
                logging.exception("Hotkey on_activate failed")
        # Secondary tap combo (fires once per press, ignores auto-repeat).
        if (not self._sec_active and self.secondary and self.on_secondary
                and self.secondary <= self._pressed):
            self._sec_active = True
            try:
                self.on_secondary()
            except Exception:
                logging.exception("Secondary hotkey callback failed")

    def _on_release(self, key):
        name = _key_name(self._listener, key)
        if name is None:
            return
        self._pressed.discard(name)
        if self._active and name in self.combo:
            self._active = False
            try:
                self.on_deactivate()
            except Exception:
                logging.exception("Hotkey on_deactivate failed")
        if self._sec_active and name in self.secondary:
            self._sec_active = False
