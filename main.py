"""
main.py — Talkloom entry point.

Wires together: config → transcriber → recorder → hotkey listener → tray.

Threading model
---------------
  * Qt event loop        : main thread (tray icon, dialogs).
  * pynput listener      : its own thread; hotkey callbacks fire there.
  * VAD worker           : background thread per recording (audio.py).
  * transcribe pipeline  : a fresh daemon thread per utterance, so a slow
                           transcription never blocks the Qt loop or the
                           hotkey listener.
State changes and notifications cross into the GUI thread via Qt signals.
"""

# Corporate proxies (Zscaler etc.) re-sign TLS traffic with their own root CA.
# truststore makes Python validate against the Windows certificate store —
# where IT has installed that CA — instead of certifi's bundle. Must run
# before anything imports httpx/requests.
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass  # not installed — fine on non-corporate machines

import logging
import os
import queue
import sys
import threading
import time

from PySide6.QtCore import QLockFile, QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication, QMessageBox, QProgressDialog

import cleanup
import dictionary
import history
import inject
import settings
import stats
from audio import Recorder
from hotkey import HotkeyListener
from transcribe import Transcriber, model_is_cached
from tray import Tray


# How long the mic stays open after the hotkey is released (see
# _finish_recording).
TAIL_SECONDS = 0.2


class _LoaderSignals(QObject):
    finished = Signal(bool, str)   # ok, error message


class AppController:
    """Owns all the pieces; passed around so the tray/dialog can reach them."""

    def __init__(self, qt_app: QApplication):
        self.qt_app = qt_app
        self.config = settings.Config()
        self.transcriber = Transcriber(
            self.config["model_size"], self.config["language"],
            punctuation_hint=self.config["punctuation_hint"],
        )
        self.recorder = Recorder()
        self.hotkeys = HotkeyListener(
            self.config["hotkey"],
            on_activate=self._on_hotkey_down,
            on_deactivate=self._on_hotkey_up,
        )
        self.tray = Tray(self)
        # The overlay reuses the tray's state signal: one emit drives both the
        # tray icon and the on-screen waveform pill.
        from overlay import Overlay
        self.overlay = Overlay(self.recorder)
        self.tray.set_state.connect(self.overlay.apply_state)
        self.hotkeys.set_secondary(
            self.config["add_word_hotkey"], self._on_add_word_hotkey
        )
        # Pipeline state (see "dictation pipeline" below).
        self._jobs: queue.Queue = queue.Queue()   # audio awaiting transcription
        self._state_lock = threading.Lock()
        self._recording = False     # hotkey currently held / mic open
        self._rec_gen = 0           # bumps on every key-down
        self._pending = 0           # utterances not yet pasted
        self._start_thread = None
        self._stop_thread = None
        self._loader_refs = []      # keeps preload signal objects alive

    # ------------------------------------------------------- lifecycle ----

    def start(self):
        self.tray.show()
        threading.Thread(target=self._worker, name="pipeline", daemon=True).start()
        self.hotkeys.start()
        self.preload_model()
        # Watchdog: if Windows silently kills the keyboard hook (slow callback,
        # sleep/lock cycle), detect it and bring the hotkey back to life.
        self._watchdog = QTimer()
        self._watchdog.setInterval(3_000)
        self._watchdog.timeout.connect(self._check_listener)
        self._watchdog.start()

    def _check_listener(self):
        try:
            if not self.hotkeys.is_running():
                logging.warning("Hotkey listener found dead — restarting")
                self.hotkeys.restart()
            # Also un-stick keys whose release was missed (PC locked
            # mid-press etc.) so the mic can't be left open.
            self.hotkeys.reconcile()
        except Exception:
            logging.exception("Listener watchdog failed")

    def quit(self):
        try:
            self.hotkeys.stop()
        except Exception:
            pass
        self.qt_app.quit()

    # ---------------------------------------------------- model preload ---

    def preload_model(self):
        """Load Whisper in the background — at startup, and again whenever the
        model size is changed in Settings — so the next dictation is fast.
        If the model isn't cached yet, show a download progress dialog."""
        size = self.config["model_size"]
        dlg = None
        if not model_is_cached(size):
            dlg = QProgressDialog(
                f"Downloading Whisper '{size}' model from HuggingFace…\n"
                "This only happens once.", None, 0, 0
            )
            dlg.setWindowTitle("Talkloom — downloading model")
            dlg.setCancelButton(None)
            dlg.setMinimumDuration(0)
            dlg.show()

        sig = _LoaderSignals()
        # Hold a reference: without one, the signal object can be destroyed
        # (on the loader thread) before its queued 'finished' is delivered,
        # and the progress dialog would never close.
        self._loader_refs.append(sig)

        def _close(ok: bool, err: str):
            if dlg is not None:
                dlg.close()
            if sig in self._loader_refs:
                self._loader_refs.remove(sig)
            if not ok:
                self.tray.notify.emit("Talkloom", f"Model load failed — check logs")
                logging.error("Model preload failed: %s", err)

        sig.finished.connect(_close)

        def _load():
            try:
                self.transcriber.load()
                # Warm the VAD too, so the first hotkey press doesn't have to
                # load anything on the keyboard hook's critical path.
                self.recorder._ensure_vad()
                sig.finished.emit(True, "")
            except Exception as e:
                logging.exception("Model preload failed")
                sig.finished.emit(False, str(e))

        threading.Thread(target=_load, name="model-loader", daemon=True).start()

    # ------------------------------------------------- dictation pipeline -
    #
    # Key-down  -> "rec-start" thread opens the mic.
    # Key-up    -> "rec-stop" thread closes the mic and queues the audio.
    # One permanent "pipeline" worker transcribes and pastes queued audio in
    # order. So you can start the next dictation while the previous one is
    # still transcribing — nothing is thrown away.

    def _refresh_state(self):
        """Emit the state the tray/overlay should show right now."""
        with self._state_lock:
            if self._recording:
                state = "recording"
            elif self._pending:
                state = "processing"
            else:
                state = "idle"
        self.tray.set_state.emit(state)

    def _on_hotkey_down(self):
        """Runs on the pynput HOOK thread — must return in milliseconds.
        Windows silently removes keyboard hooks whose callbacks are slow
        (opening an audio stream takes 100-500 ms), which kills the hotkey
        with no error until restart. So: emit the state change and hand the
        real work to a thread immediately."""
        with self._state_lock:
            self._rec_gen += 1
            gen = self._rec_gen
            self._recording = True
        self.tray.set_state.emit("recording")
        t = threading.Thread(
            target=self._start_recording, args=(gen, self._stop_thread),
            name="rec-start", daemon=True,
        )
        self._start_thread = t
        t.start()

    def _start_recording(self, gen: int, prev_stop):
        try:
            # The previous recording keeps the mic open for a short tail
            # after key-up; let it finish closing before opening a new one.
            if prev_stop is not None:
                prev_stop.join(timeout=2)
            self.recorder.start()
        except Exception:
            logging.exception("Failed to start recording")
            with self._state_lock:
                if self._rec_gen == gen:
                    self._recording = False
            self._refresh_state()
            self.tray.notify.emit("Talkloom", "Could not open microphone — check logs")

    def _on_hotkey_up(self):
        """Also the hook thread; same rule — spawn and return."""
        with self._state_lock:
            gen = self._rec_gen
            self._pending += 1
        t = threading.Thread(
            target=self._finish_recording, args=(gen, self._start_thread),
            name="rec-stop", daemon=True,
        )
        self._stop_thread = t
        t.start()

    def _finish_recording(self, gen: int, start_thread):
        audio = None
        try:
            # Quick tap race: on a very short press, stop() could run before
            # the start thread has opened the stream, leaving it dangling.
            if start_thread is not None:
                start_thread.join(timeout=3)
            # People let go of the hotkey as they finish the last word; keep
            # the mic open a moment longer so that word is not clipped.
            time.sleep(TAIL_SECONDS)
            audio = self.recorder.stop()   # None = silence
        except Exception:
            logging.exception("Failed to stop recording")
        finally:
            with self._state_lock:
                if self._rec_gen == gen:   # no newer press has started
                    self._recording = False
                if audio is None:
                    self._pending -= 1
            if audio is not None:
                self._jobs.put(audio)
            self._refresh_state()

    def _worker(self):
        """Permanent thread: one utterance at a time, in order."""
        while True:
            audio = self._jobs.get()
            # Never crash the tray app: everything is wrapped.
            try:
                self._transcribe_and_paste(audio)
            except Exception:
                logging.exception("Transcription pipeline failed")
                self.tray.notify.emit("Talkloom", "Transcription failed — check logs")
            finally:
                with self._state_lock:
                    self._pending -= 1
                self._refresh_state()

    def _transcribe_and_paste(self, audio):
        text = self.transcriber.transcribe(
            audio, hotwords=dictionary.hotwords(self.config["dictionary"])
        )
        if not text.strip():
            return

        text = cleanup.clean(text, commands=self.config["voice_commands"])
        text, corrections = dictionary.correct(text, self.config["dictionary"])
        if text:
            # If the user is already holding the hotkey for the NEXT
            # dictation, wait for them to finish: pasting now would send
            # Ctrl+V with their modifiers held (a different shortcut).
            deadline = time.time() + 120
            while self._recording and time.time() < deadline:
                time.sleep(0.05)
            inject.inject(text, wait_released=self.hotkeys.all_released)
            stats.record(
                words=len(text.split()),
                chars=len(text),
                speech_seconds=len(audio) / 16000,
                corrections=len(corrections),
            )
            if self.config["keep_history"]:
                history.append(text)
        if corrections:
            # Tell the user exactly what was substituted so bad matches
            # are caught immediately (full detail is also in log.txt).
            shown = "; ".join(f"{o} → {r}" for o, r in corrections[:4])
            if len(corrections) > 4:
                shown += f" (+{len(corrections) - 4} more)"
            self.tray.notify.emit("Dictionary corrections", shown)

    def _on_add_word_hotkey(self):
        """Hook thread — spawn and return, same rule as the dictation hotkey."""
        threading.Thread(target=self._add_word, name="add-word", daemon=True).start()

    def _add_word(self):
        try:
            text = dictionary.capture_selection(
                wait_released=self.hotkeys.all_released
            )
            if not dictionary.valid_entry(text):
                self.tray.notify.emit(
                    "Talkloom", "Select a single word or short phrase first"
                )
                return
            words = self.config["dictionary"]
            if text in words:
                self.tray.notify.emit("Talkloom", f'"{text}" is already in the dictionary')
                return
            words.append(text)
            self.config.save()
            logging.info("Dictionary add: %r (%d entries)", text, len(words))
            self.tray.notify.emit("Talkloom", f'Added "{text}" to dictionary')
        except Exception:
            logging.exception("Add-word failed")
            self.tray.notify.emit("Talkloom", "Could not read selection — check logs")


def main():
    settings.setup_logging()
    logging.info("Talkloom starting")

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)   # closing dialogs must not exit the tray app

    # Single instance: two copies would each hook the keyboard, so every
    # dictation would be recorded and pasted twice. A stale lock left by a
    # crash is detected (the owning process no longer exists) and taken over.
    lock = QLockFile(os.path.join(settings.app_data_dir(), "talkloom.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        logging.info("Another Talkloom is already running — exiting")
        QMessageBox.information(
            None, "Talkloom",
            "Talkloom is already running.\n\n"
            "Look for its icon in the system tray (bottom-right of the screen).",
        )
        sys.exit(0)

    ctrl = AppController(app)
    ctrl.start()

    code = app.exec()
    lock.unlock()
    sys.exit(code)


if __name__ == "__main__":
    main()
