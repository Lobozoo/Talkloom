"""
keytest.py — diagnostic: prints exactly what pynput sees for every key event,
plus the normalised name Talkloom's hotkey.py would use. Run it, hold your
dictation hotkey, release, then press Esc to quit.

The combo it watches for is read from your Talkloom config, so it always
matches the hotkey the app is really using.
"""

from pynput import keyboard

from hotkey import _key_name  # reuse the exact same normalisation Talkloom uses
from settings import Config

config = Config()
COMBO = set(config["hotkey"])
ADD_WORD = set(config["add_word_hotkey"])
COMBO_LABEL = "+".join(config["hotkey"])

pressed = set()


def on_press(key):
    name = _key_name(listener, key)
    pressed.add(name)
    print(f"PRESS   raw={key!r:20} -> name={name!r:10}  currently down: {sorted(str(p) for p in pressed)}")
    if COMBO and COMBO <= pressed:
        print(">>> COMBO DETECTED — Talkloom would start recording here <<<")
    if ADD_WORD and ADD_WORD <= pressed:
        print(">>> ADD-WORD COMBO DETECTED <<<")


def on_release(key):
    name = _key_name(listener, key)
    pressed.discard(name)
    print(f"RELEASE raw={key!r:20} -> name={name!r}")
    if key == keyboard.Key.esc:
        print("Esc pressed — exiting.")
        return False


listener = keyboard.Listener(on_press=on_press, on_release=on_release)
listener.start()
print(f"Listening… hold {COMBO_LABEL}, then press Esc to quit.\n")
listener.join()
