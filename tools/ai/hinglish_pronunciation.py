"""
Word-level pronunciation fixes applied to voiceover text right before it is sent to
ElevenLabs. This is a safety net for two cases the Devanagari-in-vo_text prompt rule
(see pipeline_runner._VOICEOVER_RULES) can't cover:

  1. The brand name and other coined/invented words — they have no real Devanagari
     spelling to fall back on, so they need a direct respelling instead.
  2. Text that bypasses the script LLM entirely (e.g. a human typing a manual edit
     into the voiceover field at the keyframe-approval gate).

Entries are plain find/replace pairs, matched whole-word and case-insensitively, so this
is functionally identical to an ElevenLabs "alias" pronunciation-dictionary rule — just
local and git-versioned instead of managed via the ElevenLabs API. Tune by ear: generate
a clip, listen, and adjust the replacement spelling until it reads correctly.
"""
from __future__ import annotations

import re

# lowercase word -> respelled text fed to ElevenLabs instead.
PRONUNCIATION_FIXES: dict[str, str] = {
    # Brand name: intended as 2 syllables "to-taa". "Toteaa" gets read as 3 syllables
    # ("to-te-aa") because ElevenLabs treats the middle "e" as its own vowel sound.
    "toteaa": "Totaa",
}

_WORD_RE = {
    word: re.compile(rf"(?<!\w){re.escape(word)}(?!\w)", re.IGNORECASE)
    for word in PRONUNCIATION_FIXES
}


def apply_pronunciation_fixes(text: str) -> str:
    """Replace known-problem words with a respelling ElevenLabs pronounces correctly.
    Preserves the original capitalization pattern (all-caps / capitalized / lowercase)."""
    if not text:
        return text
    for word, pattern in _WORD_RE.items():
        replacement = PRONUNCIATION_FIXES[word]

        def _sub(m: re.Match, replacement: str = replacement) -> str:
            matched = m.group(0)
            if matched.isupper():
                return replacement.upper()
            if matched[0].isupper():
                return replacement[0].upper() + replacement[1:]
            return replacement[0].lower() + replacement[1:]

        text = pattern.sub(_sub, text)
    return text
