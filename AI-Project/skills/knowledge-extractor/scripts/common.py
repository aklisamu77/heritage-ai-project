"""
common.py — shared helpers for prepare.py and check.py (standard library only).

Matching is done on a normalized view of the text: diacritics, tatweel and invisible marks removed,
letter variants unified (أ إ آ ٱ → ا, ى ی → ي, ة → ه, ک → ك), digits unified, punctuation dropped,
whitespace collapsed. Every normalized character keeps a map back to its original position, so a
quote found in normalized text resolves to exact character offsets in the source.
"""

import json
import re

HARAKAT = set(chr(c) for c in list(range(0x064B, 0x0653)) + [0x0670] + list(range(0x06D6, 0x06EE)))
INVISIBLE = {"ـ", "‏", "‎", "‌", "‍", "﻿"}
LETTER_MAP = {"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ی": "ي", "ة": "ه", "ک": "ك"}
DIGIT_MAP = {c: str(i) for s in ("٠١٢٣٤٥٦٧٨٩", "۰۱۲۳۴۵۶۷۸۹") for i, c in enumerate(s)}
ARABIC_LETTER = re.compile(r"[ء-يٱ-ۓ]")

# kinds of speaker a knowledge object can be attributed to, and whose voice each one is
AUTHOR_VOICE = {"author", "prophet", "quran", "quoted"}   # words found in the author's text
EDITOR_VOICE = {"editor"}                                # words found in the editor's notes/headings
NAME_REQUIRED = {"prophet", "quoted"}                     # the text must name the speaker


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, obj):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


def normalize(text):
    """(normalized string, list mapping each normalized char to its index in text)"""
    out, idx, space = [], [], False
    for i, ch in enumerate(text):
        if ch in HARAKAT or ch in INVISIBLE:
            continue
        ch = DIGIT_MAP.get(ch, LETTER_MAP.get(ch, ch))
        if ch.isalnum():
            out.append(ch)
            idx.append(i)
            space = False
        elif out and not space:          # any run of spaces / punctuation becomes one space
            out.append(" ")
            idx.append(i)
            space = True
    while out and out[-1] == " ":
        out.pop()
        idx.pop()
    return "".join(out), idx


def norm(text):
    return normalize(text)[0].strip()


def letters(text):
    return len(ARABIC_LETTER.findall(text))
