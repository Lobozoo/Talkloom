r"""
transcribe.py — faster-whisper (CTranslate2) wrapper.

The model is loaded once and kept in memory; reloading per recording would add
seconds of latency. Changing model size in Settings just marks the model dirty
and it is reloaded lazily on the next transcription.

Models are cached in %APPDATA%\Talkloom\models\ — faster-whisper's built-in
HuggingFace download kicks in automatically on first use of a given size.
"""

import logging
import os
import threading

import numpy as np

from settings import models_dir


def _detect_device() -> tuple[str, str]:
    """Return (device, compute_type): CUDA if available, else CPU int8."""
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda", "float16"
    except Exception:
        pass
    # int8 on CPU is the sweet spot for speed vs accuracy.
    return "cpu", "int8"


def model_is_cached(size: str) -> bool:
    """Rough check whether a model of this size already exists on disk,
    so the UI knows whether to show a download progress dialog."""
    root = models_dir()
    try:
        for entry in os.listdir(root):
            if size in entry.lower():
                # model dir must contain the CT2 weights file
                inner = os.path.join(root, entry)
                for dirpath, _dirs, files in os.walk(inner):
                    if "model.bin" in files:
                        return True
    except Exception:
        pass
    return False


# Whisper imitates the style of the text it is shown before the audio. A
# short, properly punctuated example nudges it to write full sentences with
# commas and capitals — the smaller models otherwise sometimes drift into
# unpunctuated lowercase. Works alongside the dictionary hotwords.
PUNCTUATION_HINT = (
    "The following is a dictated note. It is written in complete sentences, "
    "with proper punctuation, commas, and capital letters."
)


class Transcriber:
    def __init__(self, model_size: str = "small", language: str | None = "en",
                 punctuation_hint: bool = True):
        self.model_size = model_size
        self.initial_prompt = PUNCTUATION_HINT if punctuation_hint else None
        # Pinning the language skips Whisper's per-utterance auto-detection,
        # which is unreliable on short clips (it guesses from ~1 s of audio).
        # None = auto-detect.
        self.language = language
        self._model = None
        self._loaded_size: str | None = None   # size of the model in memory
        self._lock = threading.Lock()   # model load + inference are serialised

    def set_model_size(self, size: str):
        # Deliberately lock-free: this is called from the GUI thread (Settings
        # → Save). Taking self._lock here would freeze the window for as long
        # as a transcription is running. _load_locked() notices the change.
        if size != self.model_size:
            self.model_size = size
            logging.info("Model size changed to %s (will reload)", size)

    def load(self):
        """Load (and download if needed) the model. Safe to call repeatedly."""
        with self._lock:
            self._load_locked()

    def _load_locked(self):
        size = self.model_size
        if self._model is not None and self._loaded_size == size:
            return
        from faster_whisper import WhisperModel

        device, compute_type = _detect_device()
        logging.info(
            "Loading Whisper '%s' on %s (%s)…", size, device, compute_type
        )
        self._model = None              # free the old model before loading
        self._model = WhisperModel(
            size,
            device=device,
            compute_type=compute_type,
            download_root=models_dir(),
        )
        self._loaded_size = size
        logging.info("Whisper model ready")

    def transcribe(self, audio: np.ndarray, hotwords: str | None = None) -> str:
        """audio: float32 mono @16 kHz. Returns the raw transcript text.
        hotwords: optional biasing string (custom dictionary words)."""
        with self._lock:
            self._load_locked()
            segments, _info = self._model.transcribe(
                audio,
                language=self.language, # pinned (config) or None = auto-detect
                hotwords=hotwords,      # nudge decoding toward dictionary words
                initial_prompt=self.initial_prompt,   # punctuation style hint
                vad_filter=True,        # second-stage VAD trims residual silence
                beam_size=5,
            )
            # segments is a generator — consuming it runs the actual decode.
            return " ".join(seg.text.strip() for seg in segments).strip()
