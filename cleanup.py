"""
cleanup.py — rule-based transcript post-processing.

Runs instantly in-process; no network, no external dependencies.

Whisper already punctuates and capitalises reasonably well. This module only
repairs the things it gets wrong for dictation, and the damage our own filler
removal would otherwise leave behind. Every rule is deliberately conservative:
when in doubt, the text is left exactly as Whisper wrote it.

What it does, in order:
  1. Fillers       strips um / uh / uhm / erm / er, and phrase fillers
                   ("you know", "like") only when set off by commas or at a
                   sentence start — "I like it" is left untouched.
  2. Stutters      "I I think", "the, the plan" -> one word. Only for a short
                   list of words that are never doubled on purpose.
  3. Pronoun       a lone "i" / "i'm" -> "I" / "I'm".
  4. Punctuation   no space before , . ! ? ; :  — one space after a comma —
                   no doubled or orphaned commas — ".." -> "." (a real "..."
                   is kept).
  +  Commands      saying "new line" / "new paragraph" inserts a line break
                   (can be switched off in Settings).
  5. Conjunctions"Do so. And then go." -> "Do so, and then go." — a
                   sentence never starts with and / but / or.
  6. Capitals      first word of the text and of each sentence — but NOT
                   after an abbreviation ("e.g. a hammer", "at 5 p.m. we").
"""

import re

# ------------------------------------------------------------- fillers -----

# A filler must stand alone: not glued to a hyphen or apostrophe, so the
# "uh" in "uh-huh" (which means yes) is never touched.
_L = r"(?<![\w'’-])"      # nothing word-like to the left
_R = r"(?![\w'’-])"       # nothing word-like to the right

# um, umm, uhm, uh, uhh, erm — any casing. "er" is matched case-sensitively
# ("er" / "Er") so that "ER" (emergency room) survives.
_FILL = rf"{_L}(?:(?i:u+h*m+|u+h+|e+r+m+)|[Ee]r+){_R}"

# Three patterns, applied in order, so the punctuation around a filler comes
# out right:
#   "It was, um, good"       -> "It was good"        (both commas go)
#   "I went, um. Then left"  -> "I went. Then left"  (the full stop stays)
#   "um so yeah"             -> "so yeah"
_FILLER_IN_COMMAS = re.compile(rf",\s*{_FILL}\s*,\s*")
_FILLER_BEFORE_STOP = re.compile(rf",?\s*{_FILL}\s*([.!?])")
_PURE_FILLERS = re.compile(rf"{_FILL},?\s*")

# Phrase fillers — only removed when set off by commas or at sentence start,
# which is how Whisper almost always transcribes genuine spoken fillers.
_PHRASE = r"(?:like|you know|sort of|kind of|I mean)"
_PHRASE_FILLERS = re.compile(
    rf"(?:,\s*{_PHRASE}\s*,)"
    rf"|(?:(?<=[.!?]\s){_PHRASE},\s*)"
    rf"|(?:^{_PHRASE},\s*)",
    re.IGNORECASE,
)

# ------------------------------------------------------------ stutters -----

# Words that are never said twice in a row on purpose. Deliberately excludes
# "that" ("I know that that is"), "had" ("he had had"), "is" ("what it is
# is"), "in"/"on" ("come on on Monday"), "very", "no", "so".
_STUTTER_WORDS = (
    "I|the|a|an|and|to|of|we|you|it|but|my|for|with|this|if|or|at|be"
)
_STUTTER = re.compile(
    rf"{_L}({_STUTTER_WORDS})(?:,?\s+\1{_R})+", re.IGNORECASE
)

# ------------------------------------------------------------- pronoun -----

# A lone lowercase "i", or "i'm" / "i've" / "i'll" / "i'd". Not "i.e.", not
# a list marker "(i)", not part of a hyphenated word.
_LONE_I = re.compile(r"(?<![\w'’.(-])i(?![\w.)-])")

# --------------------------------------------------------- punctuation -----

_P = r",.!?;:"
# Space before punctuation — only when the mark really ends something, so
# " .NET" and " .5" are not glued onto the previous word.
_SPACE_BEFORE_PUNCT = re.compile(rf"\s+([{_P}])(?=\s|$|[{_P}\"')\]”’])")
_DOUBLE_COMMA = re.compile(r",(?:\s*,)+")
_COMMA_BEFORE_STOP = re.compile(r",\s*([.!?])")
_DOUBLE_STOP = re.compile(r"(?<!\.)\.\.(?!\.)")            # ".." but not "..."
# A comma or ! ? jammed against the next word. Digits are left alone
# ("1,000"), and so is a full stop ("example.com", "3.5").
_NO_SPACE_AFTER = re.compile(r"([,!?])(?=[^\W\d_])")
_LEADING_JUNK = re.compile(r"^[\s,;:]*(?:\.\s+)?")

# ------------------------------------------------------ voice commands -----

# Saying "new line" or "new paragraph" inserts a real line break — the one
# piece of formatting Whisper cannot work out by itself. Punctuation Whisper
# wrapped around the command ("First item. New line. Second item.") is
# swallowed with it.
_COMMAND = re.compile(
    rf"(?P<lead>[,;:]?\s*){_L}(?P<cmd>new\s?line|new\s+paragraph){_R}"
    rf"(?P<trail>\s*[.,;:]?\s*)",
    re.IGNORECASE,
)
# "...a new line of products", "start the new paragraph": when one of these
# words comes straight before, it is ordinary speech, not a command.
_NOT_COMMAND_AFTER = {
    "a", "the", "this", "that", "another", "one", "each", "every", "per",
    "any", "some", "no", "our", "my", "your", "his", "her", "their", "its",
    "whole", "entire", "first", "second", "next", "last", "brand", "same",
}
_PREV_WORD = re.compile(r"([A-Za-z']+)$")
_NEWLINE_THEN_LOWER = re.compile(r"(\n)([a-z])")


def _apply_commands(text: str) -> str:
    def repl(m: re.Match) -> str:
        if not m.group("lead").strip():          # no comma etc. before it
            prev = _PREV_WORD.search(text[:m.start()])
            if prev and prev.group(1).lower() in _NOT_COMMAND_AFTER:
                return m.group(0)
        if not m.group("trail").strip() and \
                re.match(r"of\b", text[m.end():], re.IGNORECASE):
            return m.group(0)                    # "new line of ..."
        return "\n\n" if "paragraph" in m.group("cmd").lower() else "\n"

    text = _COMMAND.sub(repl, text)
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)     # no spaces around breaks
    text = re.sub(r"\n{3,}", "\n\n", text)
    return _NEWLINE_THEN_LOWER.sub(lambda m: m.group(1) + m.group(2).upper(), text)


# -------------------------------------------------------- conjunctions -----

# Whisper likes to chop one spoken thought into two sentences at a breath:
# "Please do so. And then I will delete it." A sentence should not start
# with and / but / or, so the two are joined back up with a comma:
# "Please do so, and then I will delete it." Only after a plain full stop —
# never after "?", "!" or "...".
_CONJUNCTION_SPLIT = re.compile(r"(?<!\.)\.[ \t]+(And|But|Or)\b")

# ------------------------------------------------------------ capitals -----

# Abbreviations whose full stop does not end a sentence. Compared without
# the final dot, lowercased.
_ABBREVIATIONS = {
    "e.g", "eg", "i.e", "ie", "etc", "vs", "cf", "approx", "est",
    "a.m", "p.m",
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st",
    "inc", "ltd", "co", "dept", "fig", "min", "max",
}
# Sentence end (+ optional closing quote/bracket), whitespace, lowercase letter.
_SENTENCE_START = re.compile(r"([.!?])([\"')\]”’]?\s+)([a-z])")
_LAST_WORD = re.compile(r"([A-Za-z][A-Za-z.]*)$")


def _capitalise_sentences(text: str) -> str:
    def fix(m: re.Match) -> str:
        if m.group(1) == ".":
            if text[:m.start()].endswith(".."):
                return m.group(0)                # "Well... maybe" — a pause
            prev = _LAST_WORD.search(text[:m.start()])
            if prev and prev.group(1).lower() in _ABBREVIATIONS:
                return m.group(0)                # "e.g. a hammer" — leave it
        return m.group(1) + m.group(2) + m.group(3).upper()

    text = _SENTENCE_START.sub(fix, text)
    if text and text[0].islower():
        text = text[0].upper() + text[1:]
    return text


# ---------------------------------------------------------------- main -----

def clean(text: str, commands: bool = True) -> str:
    """commands: turn spoken "new line" / "new paragraph" into line breaks."""
    if not text:
        return ""

    # 1. fillers
    text = _FILLER_IN_COMMAS.sub(" ", text)
    text = _FILLER_BEFORE_STOP.sub(r"\1", text)
    text = _PURE_FILLERS.sub("", text)
    text = _PHRASE_FILLERS.sub(lambda m: "," if m.group(0).startswith(",") else "", text)

    # 2. stutters   3. pronoun
    text = _STUTTER.sub(r"\1", text)
    text = _LONE_I.sub("I", text)

    # 4. punctuation
    text = re.sub(r"\s{2,}", " ", text)
    text = _SPACE_BEFORE_PUNCT.sub(r"\1", text)
    text = _DOUBLE_COMMA.sub(",", text)
    text = _COMMA_BEFORE_STOP.sub(r"\1", text)
    text = _DOUBLE_STOP.sub(".", text)
    text = _NO_SPACE_AFTER.sub(r"\1 ", text)
    # A removed filler can leave orphaned punctuation at the very start.
    text = _LEADING_JUNK.sub("", text).strip()

    # voice commands (after the whitespace tidy-up, which would eat the breaks)
    if commands:
        text = _apply_commands(text)

    # 5. capitals — first, re-join sentences split before and / but / or
    def join(m: re.Match) -> str:
        prev = _LAST_WORD.search(text[:m.start()])
        # "5 p.m. And then" -> "5 p.m., and then": the abbreviation keeps its dot.
        dot = "." if prev and prev.group(1).lower() in _ABBREVIATIONS else ""
        return dot + ", " + m.group(1).lower()

    text = _CONJUNCTION_SPLIT.sub(join, text)
    return _capitalise_sentences(text)
