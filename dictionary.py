"""
dictionary.py — custom vocabulary support.

Three jobs:
  * capture_selection(): grab whatever text the user has selected in any app
    (simulated Ctrl+C, read clipboard, restore original clipboard).
  * hotwords(): build the biasing string fed to faster-whisper so the decoder
    is nudged toward dictionary words during transcription.
  * correct(): conservative fuzzy post-correction — repairs near-miss
    transcriptions ("kill gala lock" -> "Kilgallioch") using difflib.
    Every correction is returned to the caller for logging, so bad
    corrections are visible and the threshold can be tuned.
"""

import difflib
import logging
import time

import pyperclip
from pynput.keyboard import Controller, Key

_kb = Controller()

# Similarity threshold for a correction. Deliberately conservative: too low
# and legitimate words get "corrected" into jargon; too high and variants
# slip through. Tune with the log lines if needed.
MIN_RATIO = 0.78


# ----------------------------------------------------------- capture --------

def capture_selection(wait_released=None) -> str:
    """Copy the current selection in the focused app and return it.
    Waits for the user's hotkey fingers to lift first (same reason as
    inject.py: Ctrl+C sent while extra modifiers are held misfires)."""
    if wait_released is not None:
        deadline = time.time() + 2.0
        while time.time() < deadline and not wait_released():
            time.sleep(0.02)

    try:
        original = pyperclip.paste()
    except Exception:
        original = None

    try:
        pyperclip.copy("")           # clear, so an app that ignores Ctrl+C
    except Exception:                # yields "" rather than stale clipboard
        pass

    with _kb.pressed(Key.ctrl):
        _kb.press("c")
        _kb.release("c")
    time.sleep(0.15)                 # give the app a beat to service the copy

    try:
        text = pyperclip.paste() or ""
    except Exception:
        text = ""

    if original is not None:
        try:
            pyperclip.copy(original)
        except Exception:
            pass
    return text.strip()


def valid_entry(text: str) -> bool:
    """A dictionary entry is a word or short phrase, not a paragraph."""
    return bool(text) and len(text) <= 60 and "\n" not in text \
        and len(text.split()) <= 4


# ---------------------------------------------------------- hotwords --------

def hotwords(words: list[str]) -> str | None:
    """faster-whisper accepts a free-text hotwords string that biases the
    decoder. Effectiveness fades as the list grows very large."""
    return ", ".join(words) if words else None


# --------------------------------------------------------- correction -------

# Fuzzy matching is only attempted for dictionary words at least this long.
# Shorter words are one letter away from too many ordinary words
# ("Teams" vs "team"), so they only ever get their casing fixed.
FUZZY_MIN_LEN = 6


def _affix(tok: str) -> tuple[str, str, str]:
    """Split a token into (leading punctuation, core, trailing punctuation)
    so brackets, quotes and full stops survive a replacement. A possessive
    's is treated as trailing: "Crookhil's" -> ("", "Crookhil", "'s")."""
    i = 0
    while i < len(tok) and not tok[i].isalnum():
        i += 1
    j = len(tok)
    while j > i and not tok[j - 1].isalnum():
        j -= 1
    core = tok[i:j]
    for poss in ("'s", "’s"):
        if core.lower().endswith(poss) and len(core) > len(poss):
            j -= len(poss)
            core = tok[i:j]
            break
    return tok[:i], core, tok[j:]


def _strip(tok: str) -> str:
    return _affix(tok)[1].lower()


def _plural_variant(a: str, b: str) -> bool:
    """True if a and b differ only by a plural ending — a real word form,
    not a mis-transcription."""
    return a + "s" == b or b + "s" == a or a + "es" == b or b + "es" == a


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def _wrap(first_tok: str, word: str, last_tok: str) -> str:
    """The replacement word, keeping the punctuation that surrounded the
    original token(s)."""
    return _affix(first_tok)[0] + word + _affix(last_tok)[2]


def _correct_single(tokens: list[str], word: str, corrections: list):
    target = word.lower()
    fuzzy_ok = len(target) >= FUZZY_MIN_LEN

    # Split words — one dictionary word heard as two or three
    # ("Crookhill" -> "crook hill"). The de-spaced window must be about the
    # same length as the target and start the same way, which stops a window
    # that merely contains the target plus a stray word from matching.
    i = 0
    while fuzzy_ok and i < len(tokens) - 1:
        best = None
        for k in (3, 2):
            win = tokens[i:i + k]
            if len(win) < k or any(" " in t for t in win):
                continue
            joined = "".join(_strip(t) for t in win)
            if abs(len(joined) - len(target)) > 2 or joined[:2] != target[:2]:
                continue
            r = _ratio(joined, target)
            # If one token of the window is already the better match on its
            # own ("crookhill is"), leave it to the single-token pass below —
            # otherwise the neighbouring word would be swallowed.
            if any(_ratio(_strip(t), target) >= r for t in win):
                continue
            if r >= MIN_RATIO and (best is None or r > best[0]):
                best = (r, k)
        if best is not None:
            k = best[1]
            win = tokens[i:i + k]
            corrections.append((" ".join(win), word))
            tokens[i:i + k] = [_wrap(win[0], word, win[-1])]
        i += 1

    for i, tok in enumerate(tokens):
        if " " in tok:               # already replaced by a multi-word entry
            continue
        core = _affix(tok)[1]
        bare = core.lower()
        if bare == target:
            # Exact word, wrong casing — restore canonical casing.
            if core != word:
                corrections.append((tok, word))
                tokens[i] = _wrap(tok, word, tok)
            continue
        # Short tokens are too easy to false-match ("kilo" vs "kil…").
        if not fuzzy_ok or len(bare) < 4 or _plural_variant(bare, target):
            continue
        if _ratio(bare, target) >= MIN_RATIO:
            corrections.append((tok, word))
            tokens[i] = _wrap(tok, word, tok)


def _correct_multi(tokens: list[str], word: str, corrections: list):
    # Multi-word entries. Three cases, checked in order:
    #   1. exact match (fix casing only),
    #   2. split words — "Crookhill T10" heard as "crook hill t10":
    #      an (n+1)-token window whose DE-SPACED text has the same
    #      length as the de-spaced target. The length rule is what
    #      stops a window that merely contains the target plus a
    #      stray word ("at crookhill t10") from matching.
    #   3. same-length fuzzy window (misspellings).
    target = word.lower()
    n = len(target.split())
    tgt_nospace = target.replace(" ", "")
    i = 0
    while i <= len(tokens) - n:
        gram_toks = tokens[i:i + n]
        gram = " ".join(_strip(t) for t in gram_toks)

        if gram == target:                              # case 1
            if " ".join(_affix(t)[1] for t in gram_toks) != word:
                corrections.append((" ".join(gram_toks), word))
                tokens[i:i + n] = [_wrap(gram_toks[0], word, gram_toks[-1])]
            i += 1
            continue

        if i + n + 1 <= len(tokens):                    # case 2
            g2_toks = tokens[i:i + n + 1]
            g2 = "".join(_strip(t) for t in g2_toks)
            first = _strip(g2_toks[0])
            # A genuine split starts with a fragment of the target's
            # first word — this stops a stray leading word ("at")
            # that merely length-balances the window from matching.
            if first and tgt_nospace.startswith(first) and \
                    abs(len(g2) - len(tgt_nospace)) <= 1 and \
                    _ratio(g2, tgt_nospace) >= MIN_RATIO:
                corrections.append((" ".join(g2_toks), word))
                tokens[i:i + n + 1] = [_wrap(g2_toks[0], word, g2_toks[-1])]
                i += 1
                continue

        if _ratio(gram, target) >= MIN_RATIO:           # case 3
            corrections.append((" ".join(gram_toks), word))
            tokens[i:i + n] = [_wrap(gram_toks[0], word, gram_toks[-1])]
        i += 1


def correct(text: str, words: list[str]) -> tuple[str, list[tuple[str, str]]]:
    """Repair near-miss transcriptions of dictionary words.
    Returns (corrected_text, [(original, replacement), ...])."""
    if not text or not words:
        return text, []

    # Line breaks (from the "new line" voice command) must survive: the
    # token split/join below would flatten them, so go line by line.
    if "\n" in text:
        lines, all_corrections = [], []
        for line in text.split("\n"):
            fixed, found = correct(line, words)
            lines.append(fixed)
            all_corrections.extend(found)
        return "\n".join(lines), all_corrections

    tokens = text.split()
    corrections: list[tuple[str, str]] = []

    for word in words:
        word = word.strip()
        if not word:
            continue
        if len(word.split()) == 1:
            _correct_single(tokens, word, corrections)
        else:
            _correct_multi(tokens, word, corrections)

    for orig, repl in corrections:
        logging.info("Dictionary correction: %r -> %r", orig, repl)
    return " ".join(tokens), corrections
