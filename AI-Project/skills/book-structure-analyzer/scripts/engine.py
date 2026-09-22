#!/usr/bin/env python3
"""
engine.py — generic, config-driven structure engine for OCR'd Arabic heritage books.

The engine applies the SAME rules to every book. Everything book-specific lives in
two small files the AI writes after reading the book:

  book_config.json   what the book looks like (headings, running headers, patterns…)
  overrides.json     individual judgment calls the rules cannot make, each with a reason

Commands:
  python engine.py discover --source book.txt [--config book_config.json]
      Prints candidates (headings, running headers, footnote/anchor stats…) to help
      write book_config.json. Changes nothing.

  python engine.py build --source book.txt --config book_config.json
                         [--overrides overrides.json] [--pages pages.json] --out DIR
      Writes book_profile.json, structure.json, units.json, issues.json to DIR.
      Then run validate.py on DIR.

Standard library only. Offsets are Unicode code-point indices, char_end exclusive.

How it works: every character of the source gets exactly one label
(node text, a unit segment with a role, or noise). Labels are applied in layers
(regions → units → headings/numbers/footnotes/anchors/attribution → noise → overrides),
then runs of identical labels become segments / noise spans. Coverage is therefore
exact by construction; the rules decide only WHICH label a character gets.
"""

import argparse
import bisect
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict

# --------------------------------------------------------------------------- text utils

HARAKAT = set(chr(c) for c in list(range(0x064B, 0x0653)) + [0x0670] + list(range(0x06D6, 0x06EE)))
TATWEEL = "\u0640"
INVISIBLE = {"\u200f", "\u200e", "\u200c", "\u200d", "\ufeff"}
DIGIT_MAP = {}
for i, ch in enumerate("٠١٢٣٤٥٦٧٨٩"):
    DIGIT_MAP[ch] = str(i)
for i, ch in enumerate("۰۱۲۳۴۵۶۷۸۹"):
    DIGIT_MAP[ch] = str(i)
LETTER_MAP = {"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ة": "ه", "ی": "ي", "ک": "ك"}
DIGITS = "0-9٠-٩۰-۹"
ARABIC_LETTER = re.compile(r"[\u0621-\u064A\u0671-\u06D3]")
PUNCT_ONLY = re.compile(r"^[\s\-–—•·*_|.:،؛,;!?؟«»()\[\]{}\"'/\\]*$")


def is_diacritic(ch):
    return ch in HARAKAT or ch == TATWEEL


def to_int(digits):
    return int("".join(DIGIT_MAP.get(c, c) for c in digits))


class NormText:
    """Normalized view of the source (no diacritics, unified letters/digits,
    whitespace runs collapsed to one space) with a map back to source offsets."""

    def __init__(self, src):
        out, idx = [], []
        prev_space = False
        for i, ch in enumerate(src):
            if is_diacritic(ch) or ch in INVISIBLE:
                continue
            if ch.isspace():
                if prev_space:
                    continue
                out.append(" ")
                idx.append(i)
                prev_space = True
                continue
            prev_space = False
            out.append(DIGIT_MAP.get(ch, LETTER_MAP.get(ch, ch)))
            idx.append(i)
        self.text = "".join(out)
        self.idx = idx
        self.src_len = len(src)

    def from_src(self, pos):
        """first normalized index whose source position is >= pos"""
        return bisect.bisect_left(self.idx, pos)

    def to_src(self, a, b):
        """normalized [a,b) -> source [s,e)"""
        s = self.idx[a] if a < len(self.idx) else self.src_len
        e = self.idx[b - 1] + 1 if b > 0 else 0
        return s, e


def norm(s):
    return NormText(s).text.strip()


def diacritic_ratio(s):
    letters = len(ARABIC_LETTER.findall(s))
    if letters == 0:
        return 0.0
    return sum(1 for ch in s if ch in HARAKAT) / letters


def arabic_words(s):
    return [w for w in re.split(r"\s+", s) if ARABIC_LETTER.search(w)]


# --------------------------------------------------------------------------- config defaults

DEFAULTS = {
    "unit_type": "hadith",
    "body_role": "hadith_text",
    "running_headers": [],
    "unit_headings": [],                 # ordered list: str or {text, variants[], subtitle}
    "front_matter": {"role": "front_matter"},
    "back_matter": [],                   # [{type, title, role, start_at:{text, occurrence}}]
    "unit_number_prefix_regex": r"^\s*([0-9٠-٩۰-۹]{1,3})\s*[-–—ـ]\s*",
    "standalone_number_regex": r"^[\s•\-–—.]*([0-9٠-٩۰-۹]{1,3})[\s•\-–—.]*$",
    "separator_regex": r"^[\s\-–—•·*_|=]+$",
    "footnote_marker_regex": r"\(\s*([0-9٠-٩۰-۹]{1,2})\s*\)",
    "footnote_min_words": 2,
    "footnote_max_diacritic_ratio": 0.25,
    "footnote_split": {"takhrij_until": ["قوله", "وقوله", "ومعنى", "معنى"], "second_role": "gharib",
                       "first_role": "takhrij"},
    "gharib_term_regex": r"(?:قوله|ومعنى|معنى)\s*:?\s*[«(\"]?\s*([^»)\"،:]{2,40})",
    "anchors_come_after_text": True,
    "footnote_lookback": 2,
    "body_line_markers": ["ﷺ", "قال رسول الله", "عن النبي", "سمعت رسول الله"],   # a line with these is main text, not a footnote              # a footnote may belong to up to N units before the one it was read in     # a marker right after the heading belongs to the previous unit
    "attribution": {
        "keywords": ["رواه", "ورواه", "رويناه", "روياه", "متفق عليه", "حديث حسن", "حديث صحيح"],
        "role": "source_attribution",
    },
    "garbled": {
        "scripts": [[0x0E00, 0x0E7F], [0x0700, 0x074F], [0x0400, 0x04FF], [0x4E00, 0x9FFF], [0x1780, 0x17FF]],
        "latin_max_run_in_arabic": 2,
        "skip_regions": ["front_matter"],
    },
    "page_snap_window": 250,
    "digit_lookalikes": {},              # OCR letters that stand for digits in markers/numbers, e.g. {"ه": "5", "V": "7"}
    "footnote_continued_mark": "=",      # editor's sign that a footnote continues on the next page
    "main_start_at": None,               # {text, occurrence, heading}: where the main text starts, if before unit 1
    "sections": [],                      # headed sections outside the units (introduction, study…), with levels
    "expected_components": [],
    "metadata": {},
}


def load_config(path):
    cfg = json.loads(json.dumps(DEFAULTS))
    user = json.load(open(path, encoding="utf-8"))
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    def keep(x):
        return not (isinstance(x, str) and x.startswith("_help")) and not (isinstance(x, dict) and set(x) <= {"_help"})
    for key in ("unit_headings", "running_headers", "back_matter", "expected_components", "sections"):
        cfg[key] = [x for x in cfg.get(key) or [] if keep(x)]
    cfg["digit_lookalikes"] = {k: v for k, v in (cfg.get("digit_lookalikes") or {}).items() if not k.startswith("_")}
    ms = cfg.get("main_start_at")
    if isinstance(ms, dict) and "text" not in ms:
        cfg["main_start_at"] = None
    heads = []
    for h in cfg["unit_headings"]:
        if isinstance(h, str):
            h = {"text": h}
        h.setdefault("variants", [])
        heads.append(h)
    cfg["unit_headings"] = heads
    return cfg


# --------------------------------------------------------------------------- engine

# issue types that repeat per occurrence and are reported as one grouped issue
GROUPED = {
    "displaced_footnote": "Footnotes read by OCR inside another unit (usually the next one: page-bottom footnotes are read after the next page's heading). Each is assigned to its owner; see each occurrence's note for the evidence used.",
    "unanchored_footnote": "Footnotes with no matching anchor marker before them in the owner unit (the marker may be lost in OCR). Ownership rests on content terms, block order or locality only.",
    "displaced_anchor": "Footnote anchor markers read inside another unit (e.g. right after the next unit's heading).",
    "orphan_anchor": "Anchor markers with no footnote found.",
    "displaced_number": "Unit numbers read inside the previous unit.",
    "ambiguous_number": "Standalone numbers read as unit numbers; they could be page numbers.",
    "trailing_fragment": "Main text after the unit's attribution. Either legitimate (a second narration, the author's comment, a continued attribution) or a fragment displaced from a neighbouring unit — review each.",
    "manual_decision": "Judgment calls applied from overrides.json (each occurrence carries its reason).",
    "continued_footnote": "Footnotes that end with the editor's continuation mark: the rest is on the next page and is probably read as main text somewhere after — check the page image.",
}

class Engine:
    def __init__(self, src, cfg, overrides=None, pages=None):
        self.src = src
        self.N = len(src)
        self.cfg = cfg
        self.overrides = overrides or []
        self.pages_in = pages or []
        self.nt = NormText(src)
        self.lines = []  # (start, end) without newline
        pos = 0
        for ln in src.split("\n"):
            self.lines.append((pos, pos + len(ln)))
            pos += len(ln) + 1
        self.label = [None] * self.N
        self.lookalike = {k: str(v) for k, v in (cfg.get("digit_lookalikes") or {}).items()}
        extra = "".join(re.escape(k) for k in self.lookalike)
        self.rx = lambda pat: pat.replace("0-9٠-٩۰-۹", "0-9٠-٩۰-۹" + extra) if extra else pat
        self.glued_digits = []     # (s, e, kind, unit_index|None) digits glued to heading lines
        self.issues = []           # raw issues: dict(type, severity, desc, occ=list, conf)
        self.decisions = []        # what the engine decided and why (for the report)
        self.footnotes = {}        # fid -> dict(marker, owner, host, conf, reason)
        self.anchor_info = {}      # aid -> dict(marker, owner, host, pos)
        self.unit_meta = []        # per unit dicts

    # ------------------------------------------------------------ helpers
    def to_int(self, s):
        try:
            return int("".join(self.lookalike.get(c, DIGIT_MAP.get(c, c)) for c in s.strip()))
        except ValueError:
            return None

    def line_of(self, pos):
        lo, hi = 0, len(self.lines) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.lines[mid][0] <= pos:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def line_text(self, li):
        s, e = self.lines[li]
        return self.src[s:e]

    def find_all(self, text, start=0, end=None):
        """normalized occurrences of text in source[start:end] -> list of (s,e) source offsets"""
        pat = norm(text)
        if not pat:
            return []
        end = self.N if end is None else end
        out = []
        t = self.nt.text
        i = t.find(pat)
        while i != -1:
            s, e = self.nt.to_src(i, i + len(pat))
            if s >= start and e <= end:
                out.append((s, e))
            i = t.find(pat, i + 1)
        return out

    def resolve_at(self, spec, start=0, end=None):
        occ = self.find_all(spec["text"], start, end)
        if not occ:
            return None
        n = spec.get("occurrence", 1)
        if n == "last":
            return occ[-1]
        return occ[n - 1] if 0 < n <= len(occ) else None

    def set_label(self, s, e, lab, only_if=None):
        for i in range(max(0, s), min(self.N, e)):
            if only_if is None or only_if(self.label[i]):
                self.label[i] = lab

    def issue(self, typ, severity, desc, occ=None, conf=0.8, **extra):
        d = {"type": typ, "severity": severity, "description": desc, "occurrences": occ or [], "confidence": conf}
        d.update(extra)
        self.issues.append(d)
        return d

    def standalone(self, s, e, allow_digits=False):
        """occurrence fills its line(s) apart from whitespace/punctuation
        (and, with allow_digits, digits glued to it: printed page numbers)"""
        ls, le = self.lines[self.line_of(s)][0], self.lines[self.line_of(max(s, e - 1))][1]
        rest = self.src[ls:s] + self.src[e:le]
        if allow_digits:
            rest = re.sub(f"[{DIGITS}]", "", rest)
        return bool(PUNCT_ONLY.match(rest))

    def glued_digit_runs(self, s, e):
        """digit runs on the heading's line outside the heading itself"""
        ls, le = self.lines[self.line_of(s)][0], self.lines[self.line_of(max(s, e - 1))][1]
        out = []
        for a, b in ((ls, s), (e, le)):
            for m in re.finditer(f"[{DIGITS}]+", self.src[a:b]):
                out.append((a + m.start(), a + m.end(), self.src[a + m.start():a + m.end()]))
        return out

    def at_line_edge(self, s, e):
        ls, le = self.lines[self.line_of(s)][0], self.lines[self.line_of(max(s, e - 1))][1]
        return PUNCT_ONLY.match(self.src[ls:s]) or PUNCT_ONLY.match(self.src[e:le])

    def has_diacritics(self, s, e):
        return any(ch in HARAKAT for ch in self.src[s:e])

    # ------------------------------------------------------------ 1. regions
    def find_regions(self):
        cfg = self.cfg
        # back matter starts
        self.back = []
        last_head_pos = 0
        for h in cfg["unit_headings"]:
            occ = self.find_all(h["text"])
            if occ:
                last_head_pos = max(last_head_pos, occ[0][0])
        for bm in cfg["back_matter"]:
            at = self.resolve_at(bm["start_at"], start=last_head_pos)
            if at is None:
                at = self.resolve_at(bm["start_at"])
            if at is None:
                self.issue("back_matter_not_found", "high", f"back matter start '{bm['start_at']['text']}' not found", conf=0.9)
                continue
            self.back.append(dict(bm, start=self.lines[self.line_of(at[0])][0]))
        self.back.sort(key=lambda b: b["start"])
        self.main_end = self.back[0]["start"] if self.back else self.N

    # ------------------------------------------------------------ 2. headings -> unit starts
    def find_headings(self):
        heads = self.cfg["unit_headings"]
        occs = []
        for h in heads:
            o = [(s, e) for s, e in self.find_all(h["text"], 0, self.main_end)]
            occs.append(o)
        chosen = [None] * len(heads)
        kinds = [None] * len(heads)
        upper = self.main_end
        # backward greedy: latest standalone occurrence before the next unit's heading
        for k in range(len(heads) - 1, -1, -1):
            cands = [(s, e) for s, e in occs[k] if e <= upper]
            sa = [c for c in cands if self.standalone(*c)]
            sad = [c for c in cands if self.standalone(*c, allow_digits=True)]
            if sa:
                chosen[k], kinds[k] = sa[-1], "standalone"
            elif sad:
                chosen[k], kinds[k] = sad[-1], "standalone_with_page_number"
            elif cands:
                edge = [c for c in cands if self.at_line_edge(*c)]
                pick = (edge or cands)[-1]
                chosen[k], kinds[k] = pick, "glued"
            if chosen[k]:
                upper = chosen[k][0]
        # forward pass: monotonic check
        prev = -1
        for k in range(len(heads)):
            if chosen[k] is None:
                self.issue("heading_not_found", "high", f"unit heading '{heads[k]['text']}' (ordinal {k+1}) not found in order",
                           conf=0.9)
            elif chosen[k][0] <= prev:
                self.issue("heading_order", "high", f"heading '{heads[k]['text']}' out of order", conf=0.8)
            else:
                prev = chosen[k][0]
        self.head_chosen, self.head_kind, self.head_occs = chosen, kinds, occs

        # overrides can move a unit start
        self.unit_start = []
        for k in range(len(heads)):
            if chosen[k] is None:
                self.unit_start.append(None)
                continue
            self.unit_start.append(self.lines[self.line_of(chosen[k][0])][0])
        for ov in self.overrides:
            if ov.get("op") == "set_unit_start":
                k = ov["ordinal"] - 1
                at = self.resolve_at(ov["at"])
                if at is None:
                    self.issue("override_failed", "high", f"override set_unit_start: text not found: {ov['at']}", conf=1.0)
                    continue
                self.unit_start[k] = at[0]
                self.decisions.append(("set_unit_start", ov))
        # units list (skip missing headings)
        self.units = []
        valid = [k for k in range(len(heads)) if self.unit_start[k] is not None]
        for j, k in enumerate(valid):
            s = self.unit_start[k]
            e = self.unit_start[valid[j + 1]] if j + 1 < len(valid) else self.main_end
            self.units.append({"k": k, "ordinal": k + 1, "start": s, "end": e, "head": heads[k],
                               "head_span": chosen[k], "head_kind": kinds[k], "number_raw": None,
                               "issues": set(), "displaced": False})
        self.front_end = self.units[0]["start"] if self.units else self.main_end
        for i, u in enumerate(self.units):
            if u["head_kind"] == "standalone_with_page_number":
                for s, e, raw in self.glued_digit_runs(*u["head_span"]):
                    v = self.to_int(raw)
                    if v == u["ordinal"] and u["number_raw"] is None:
                        self.glued_digits.append((s, e, "number", i))
                        u["number_raw"] = raw
                    else:
                        self.glued_digits.append((s, e, "page_number", i))

        # main text may start before unit 1 (e.g. the chain of transmission of the whole book)
        self.main_start = self.front_end
        ms = self.cfg.get("main_start_at")
        if ms and self.units:
            at = self.resolve_at(ms, 0, self.front_end)
            if at is None:
                self.issue("main_start_not_found", "high", f"main_start_at text not found before unit 1: {ms['text']}", conf=0.9)
            else:
                self.main_start = at[0] if ms.get("heading", True) else self.lines[self.line_of(at[0])][0]
                self.main_head = at if ms.get("heading", True) else None
                self.front_end = self.main_start
        self.find_sections()

    # ------------------------------------------------------------ 2b. sections (outside units)
    def find_sections(self):
        """Headed sections before the main text (introduction, study, biography…), nested by level."""
        self.sections = []
        pos = 0
        for n, sc in enumerate(self.cfg.get("sections", [])):
            spec = sc.get("start_at") or {"text": sc["text"]}
            # explicit occurrence = counted over the whole front region; otherwise the first match at/after
            # the previous section (a parent without its own heading may share its first child's position)
            occ = self.find_all(spec["text"], 0 if "occurrence" in spec else pos, self.front_end)
            if not occ:
                self.issue("section_not_found", "high", f"section '{sc.get('title') or spec['text']}' not found in order", conf=0.9)
                continue
            if "occurrence" in spec:
                o = occ[spec["occurrence"] - 1] if spec["occurrence"] != "last" else occ[-1]
            else:
                sa = [o for o in occ if self.standalone(*o, allow_digits=True)]
                o = (sa or occ)[0]
            is_head = sc.get("heading", True)
            start = self.lines[self.line_of(o[0])][0] if is_head and self.standalone(*o, allow_digits=True) else o[0]
            self.sections.append({"n": n, "key": f"sec{n}", "title": sc.get("title") or spec["text"],
                                  "type": sc.get("type", "section"), "level": sc.get("level", 1),
                                  "start": start, "head": o if is_head else None, "cfg": sc})
            if is_head and self.standalone(*o, allow_digits=True):
                for s, e, raw in self.glued_digit_runs(*o):
                    self.glued_digits.append((s, e, "page_number", None))
            pos = o[0] if not is_head else o[0] + 1
        for j, sc in enumerate(self.sections):
            end = self.front_end
            for nxt in self.sections[j + 1:]:
                if nxt["level"] <= sc["level"]:
                    end = nxt["start"]
                    break
            sc["end"] = end
            sc["parent"] = None
            for prev in reversed(self.sections[:j]):
                if prev["level"] < sc["level"] and prev["start"] <= sc["start"] < prev["end"]:
                    sc["parent"] = prev["key"]
                    break

    def unit_at(self, pos):
        for i, u in enumerate(self.units):
            if u["start"] <= pos < u["end"]:
                return i
        return None

    # ------------------------------------------------------------ 3. base labels
    def base_labels(self):
        self.set_label(0, self.front_end, ("node", "front", self.cfg["front_matter"].get("role", "front_matter")))
        for sc in self.sections:   # document order: children overwrite their parent's range
            self.set_label(sc["start"], sc["end"], ("node", sc["key"], "text"))
            if sc["head"]:
                self.set_label(*sc["head"], ("node", sc["key"], "heading"))
        if self.units and self.main_start < self.units[0]["start"]:
            self.set_label(self.main_start, self.units[0]["start"], ("node", "main", "preamble"))
            if getattr(self, "main_head", None):
                self.set_label(*self.main_head, ("node", "main", "heading"))
        for i, u in enumerate(self.units):
            self.set_label(u["start"], u["end"], ("unit", i, self.cfg["body_role"], None))
        for j, b in enumerate(self.back):
            e = self.back[j + 1]["start"] if j + 1 < len(self.back) else self.N
            self.set_label(b["start"], e, ("node", f"back{j}", b.get("role", b["type"])))
        for i, u in enumerate(self.units):
            s, e = u["head_span"]
            self.set_label(s, e, ("unit", i, "heading", None))
            sub = u["head"].get("subtitle")
            if sub:
                occ = self.find_all(sub, u["start"], u["end"])
                if occ:
                    self.set_label(*occ[0], ("unit", i, "subtitle", None))

    # ------------------------------------------------------------ 4. numbers
    def numbers(self):
        pre = re.compile(self.rx(self.cfg["unit_number_prefix_regex"]))
        alone = re.compile(self.rx(self.cfg["standalone_number_regex"]))
        for li, (ls, le) in enumerate(self.lines):
            if ls < self.front_end or ls >= self.main_end:
                continue
            ui = self.unit_at(ls)
            if ui is None:
                continue
            text = self.src[ls:le]
            m = alone.match(text)
            if m and text.strip():
                v = self.to_int(m.group(1))
                u = self.units[ui]
                nxt = self.units[ui + 1] if ui + 1 < len(self.units) else None
                if v == u["ordinal"] and u["number_raw"] is None:
                    self.set_label(ls, le, ("unit", ui, "number", None))
                    u["number_raw"] = m.group(1)
                    self.issue("ambiguous_number", "low", f"standalone '{m.group(1)}' read as the number of unit {v} (could be a page number)",
                               [{"unit_id": ui, "char_start": ls, "char_end": le}], conf=0.6)
                elif nxt and v == nxt["ordinal"] and nxt["number_raw"] is None:
                    self.set_label(ls, le, ("unit", ui + 1, "number", None))
                    nxt["number_raw"] = m.group(1)
                    self.issue("displaced_number", "low", f"number '{m.group(1)}' of unit {v} read inside the previous unit",
                               [{"unit_id": ui + 1, "char_start": ls, "char_end": le}], conf=0.6)
                else:
                    self.set_label(ls, le, ("noise", "page_number"))
                continue
            m = pre.match(text)
            lab0 = self.label[ls + m.start(1)] if m else None
            if m and lab0 and lab0[0] == "unit" and lab0[2] == self.cfg["body_role"]:
                v = self.to_int(m.group(1))
                u = self.units[ui]
                if v == u["ordinal"]:
                    self.set_label(ls + m.start(), ls + m.end(), ("unit", ui, "number", None))
                    u["number_raw"] = m.group(1)

    # ------------------------------------------------------------ 5. footnotes & anchors
    def footnotes_and_anchors(self):
        cfg = self.cfg
        mk = re.compile(self.rx(cfg["footnote_marker_regex"]))
        start_re = re.compile(r"^\s*" + self.rx(cfg["footnote_marker_regex"]) + r"\s*(.*)$")
        body = cfg["body_role"]
        unit_start_lines = {self.line_of(u["start"]) for u in self.units}
        head_lines = {self.line_of(u["head_span"][0]) for u in self.units}
        fn_list, an_list = [], []
        li = 0
        L = len(self.lines)
        while li < L:
            ls, le = self.lines[li]
            if ls >= self.main_end:
                li += 1
                continue
            text = self.src[ls:le]
            m = start_re.match(text)
            node_lab = self.label[ls] if self.label[ls] and self.label[ls][0] == "node" else None
            if node_lab is None:
                k0 = next((x for x in range(ls, le) if self.label[x]), None)
                node_lab = self.label[k0] if k0 is not None and self.label[k0][0] == "node" else None
            if ls < self.main_start or node_lab:
                # outside units: footnotes and anchors belong to the enclosing node (no ownership search)
                if node_lab is None or node_lab[1] == "front" and not self.sections:
                    li += 1
                    continue
                if m and len(arabic_words(m.group(2))) >= cfg["footnote_min_words"]:
                    fe, j = le, li + 1
                    while j < L:
                        js, je = self.lines[j]
                        t2 = self.src[js:je]
                        if js >= self.main_end or start_re.match(t2) or self.label[js] != node_lab:
                            break
                        if len(arabic_words(t2)) < 2 and not re.search(f"[{DIGITS}]", t2) and not PUNCT_ONLY.match(t2):
                            break
                        fe, j = je, j + 1
                    self.set_label(ls + m.start(), fe, ("node", node_lab[1], "footnote"))
                    if self.src[ls + m.start():fe].rstrip().endswith(cfg["footnote_continued_mark"] or "\0"):
                        self.issue("continued_footnote", "medium", "footnote continues on the next page (ends with the continuation mark); its continuation may be read as main text",
                                   [{"char_start": ls + m.start(), "char_end": fe}], conf=0.8, needs_visual_check=True)
                    li = j
                    continue
                for am in mk.finditer(text):
                    s, e = ls + am.start(), ls + am.end()
                    if self.label[s] == node_lab:
                        self.set_label(s, e, ("node", node_lab[1], "anchor"))
                li += 1
                continue
            if m and len(arabic_words(m.group(2))) >= cfg["footnote_min_words"]:
                ui = self.unit_at(ls)
                fs, fe = ls + m.start(), le
                j = li + 1
                while j < L:
                    js, je = self.lines[j]
                    t = self.src[js:je]
                    if js >= self.units[ui]["end"] or j in unit_start_lines or j in head_lines:
                        break
                    if start_re.match(t):
                        break
                    if self.label[js] and self.label[js][0] == "noise":
                        break
                    if re.match(cfg["standalone_number_regex"], t) and t.strip():
                        break
                    if PUNCT_ONLY.match(t):
                        fe = je if t.strip() else fe
                        j += 1
                        continue
                    prev_s, prev_e = self.lines[j - 1]
                    if any(self.label[x] and self.label[x][0] == "noise" and self.label[x][1] == "running_header"
                           for x in range(prev_s, prev_e)):
                        break  # a running header ends the page, so the footnote cannot continue
                    tn = norm(t)
                    if "ﷺ" in t or diacritic_ratio(t) > cfg["footnote_max_diacritic_ratio"] \
                            or any(norm(mk_) in tn for mk_ in cfg["body_line_markers"]):
                        break
                    if len(arabic_words(t)) < 2 and not re.search(f"[{DIGITS}]", t):
                        break
                    fe = je
                    j += 1
                fid = len(fn_list)
                fn_list.append({"fid": fid, "marker": self.to_int(m.group(1)), "marker_raw": m.group(0).strip(),
                                "s": fs, "e": fe, "host": ui, "line": li, "end_line": j - 1})
                li = j
                continue
            # anchors in body text of this line
            for am in mk.finditer(text):
                s, e = ls + am.start(), ls + am.end()
                lab = self.label[s]
                if lab and lab[0] == "unit" and lab[2] == body:
                    an_list.append({"aid": len(an_list), "marker": self.to_int(am.group(1)), "raw": am.group(0),
                                    "s": s, "e": e, "host": lab[1]})
            li += 1
        self.fn_list, self.an_list = fn_list, an_list

        # --- leading anchors (right after heading, before any text) belong to the previous unit
        for a in an_list:
            a["owner"] = a["host"]
            if not cfg["anchors_come_after_text"] or a["host"] == 0:
                continue
            u = self.units[a["host"]]
            before = self.src[u["start"]:a["s"]]
            # strip heading/number/noise chars
            txt = "".join(ch for i, ch in enumerate(before, u["start"])
                          if self.label[i] and self.label[i][0] == "unit" and self.label[i][2] == body)
            if not arabic_words(txt):
                a["owner"] = a["host"] - 1
                a["reason"] = "anchor precedes all text of its host unit"

        # --- gharib terms per footnote
        term_re = re.compile(cfg["gharib_term_regex"])
        in_fn = bytearray(self.N)
        for f in fn_list:
            for i in range(f["s"], f["e"]):
                in_fn[i] = 1

        def unit_body_norm(ui):
            # body text only: never count the footnotes themselves as evidence
            u = self.units[ui]
            chars = [self.src[i] for i in range(u["start"], u["end"])
                     if not in_fn[i] and self.label[i] and self.label[i][0] == "unit" and self.label[i][2] == body]
            return norm("".join(chars))

        body_norm = [unit_body_norm(i) for i in range(len(self.units))]
        used = set()
        look = cfg["footnote_lookback"]
        for f in fn_list:
            ftext = norm(self.src[f["s"]:f["e"]])
            terms = []
            for tm in term_re.findall(ftext):
                w = arabic_words(tm)
                if w and len(re.sub(r"[^\u0621-\u064A]", "", w[0])) >= 3:
                    terms.append(re.sub(r"[^\u0621-\u064A]", "", w[0]))
            f["terms"] = terms
            cands = []
            for c in range(f["host"], max(-1, f["host"] - look - 1), -1):
                score, why = 0.0, []
                # an anchor can only be matched if it appears BEFORE its footnote in the text
                if any(a["owner"] == c and a["marker"] == f["marker"] and a["aid"] not in used and a["s"] < f["s"]
                       for a in an_list):
                    score += 2
                    why.append("anchor")
                if terms:
                    hit = sum(1 for tm in terms if tm in body_norm[c]) / len(terms)
                    if hit:
                        score += 4 * hit
                        why.append(f"terms {hit:.0%}")
                if c == f["host"]:
                    score += 0.5
                    why.append("local")
                cands.append((score, c, why))
            cands.sort(key=lambda x: (-x[0], x[1] != f["host"]))
            score, owner, why = cands[0]
            f["owner"], f["why"] = owner, why
            anchor = next((a for a in an_list if a["owner"] == owner and a["marker"] == f["marker"]
                           and a["aid"] not in used and a["s"] < f["s"]), None)
            if anchor:
                used.add(anchor["aid"])
                f["anchor"] = anchor["aid"]
                anchor["fid"] = f["fid"]
            has_terms = any(w.startswith("terms") for w in why)
            f["conf"] = 0.9 if (anchor and has_terms) else 0.8 if anchor else 0.65 if has_terms else 0.5

        # --- block order: footnotes printed together on one page are numbered in anchor order.
        # An unanchored footnote followed in the same block by a higher-numbered footnote owned by
        # unit U belongs to the nearest unit before U that has no footnote with that marker.
        def same_block(f, g):
            if g["line"] != f["end_line"] + 1:
                gap = self.src[f["e"]:g["s"]]
                if any(not ch.isspace() and not (self.label[f["e"] + i] and self.label[f["e"] + i][0] == "noise")
                       for i, ch in enumerate(gap)):
                    return False
            return True
        for n, f in enumerate(fn_list[:-1]):
            g = fn_list[n + 1]
            if "anchor" in f or any(w.startswith("terms") for w in f["why"]):
                continue
            if g["marker"] > f["marker"] and same_block(f, g) and g["owner"] <= f["owner"]:
                target = g["owner"] - 1
                owned = {h["marker"] for h in fn_list if h["owner"] == target and h is not f}
                if target >= 0 and f["marker"] not in owned:
                    f["owner"], f["conf"] = target, 0.55
                    f["why"] = ["block order: precedes footnote %s of unit %d" % (g["marker_raw"], g["owner"] + 1)]

        # --- labels
        fs_cfg = cfg["footnote_split"]
        split_words = [norm(w) for w in fs_cfg["takhrij_until"]]
        for f in fn_list:
            ftext_n = self.nt.text
            # locate split point in normalized space
            a_n = self.nt.from_src(f["s"])
            b_n = self.nt.from_src(f["e"])
            seg_n = ftext_n[a_n:b_n]
            cut = None
            for w in split_words:
                m = re.search(r"(?:^|[\s)\]])(" + re.escape(w) + r")\s*:", seg_n)
                if m and (cut is None or m.start(1) < cut):
                    cut = m.start(1)
            lab1 = ("unit", f["owner"], fs_cfg["first_role"], f"fn{f['fid']}")
            if cut is not None and cut > 0:
                mid = self.nt.idx[a_n + cut]
                self.set_label(f["s"], mid, lab1)
                self.set_label(mid, f["e"], ("unit", f["owner"], fs_cfg["second_role"], f"fn{f['fid']}"))
            elif cut == 0:
                self.set_label(f["s"], f["e"], ("unit", f["owner"], fs_cfg["second_role"], f"fn{f['fid']}"))
            else:
                self.set_label(f["s"], f["e"], lab1)
        for a in an_list:
            self.set_label(a["s"], a["e"], ("unit", a["owner"], "anchor", f"an{a['aid']}"))

        # --- issues
        for f in fn_list:
            occ = {"unit_id": f["owner"], "char_start": f["s"], "char_end": f["e"]}
            if cfg["footnote_continued_mark"] and self.src[f["s"]:f["e"]].rstrip().endswith(cfg["footnote_continued_mark"]):
                self.issue("continued_footnote", "medium",
                           "footnote continues on the next page; its continuation is probably read as main text further on",
                           [occ], conf=0.8, needs_visual_check=True, _fid=f["fid"])
            if f["owner"] != f["host"]:
                self.issue("displaced_footnote", "medium",
                           f"footnote {f['marker_raw']} read inside unit {f['host']+1}; assigned to unit {f['owner']+1} ({', '.join(f['why'])})",
                           [occ], conf=f["conf"], _fid=f["fid"])
            if "anchor" not in f:
                self.issue("unanchored_footnote", "medium",
                           f"footnote {f['marker_raw']} has no matching anchor marker nearby", [occ], conf=0.7, _fid=f["fid"])
        for a in an_list:
            if a["owner"] != a["host"]:
                self.issue("displaced_anchor", "medium",
                           f"anchor {a['raw']} read in unit {a['host']+1}, belongs to unit {a['owner']+1} ({a.get('reason','')})",
                           [{"unit_id": a["owner"], "char_start": a["s"], "char_end": a["e"]}], conf=0.75, _aid=a["aid"])
            if "fid" not in a:
                self.issue("orphan_anchor", "low", f"anchor {a['raw']} has no footnote",
                           [{"unit_id": a["owner"], "char_start": a["s"], "char_end": a["e"]}], conf=0.6, _aid=a["aid"])

    # ------------------------------------------------------------ 6. attribution
    def attribution(self):
        body = self.cfg["body_role"]
        kws = [norm(k) for k in self.cfg["attribution"]["keywords"]]
        role = self.cfg["attribution"]["role"]
        t = self.nt.text
        kw_alt = "|".join(re.escape(k) for k in kws)
        # work per unit on normalized text
        for i, u in enumerate(self.units):
            a_n = self.nt.from_src(u["start"])
            b_n = self.nt.from_src(u["end"])
            seg = t[a_n:b_n]
            for m in re.finditer(r"[\[(]\s*(?:" + kw_alt + r")|(?:(?<=^)|(?<= ))(?:" + kw_alt + r")", seg):
                s = self.nt.idx[a_n + m.start()]
                lab = self.label[s]
                if not (lab and lab[0] == "unit" and lab[1] == i and lab[2] == body):
                    continue
                # unbracketed keyword must start a line
                if seg[m.start()] not in "[(":
                    ls = self.lines[self.line_of(s)][0]
                    if self.src[ls:s].strip():
                        continue
                close = seg.find("]", m.end()) if seg[m.start()] in "[(" else -1
                if close != -1 and close - m.end() < 300:
                    e = self.nt.idx[a_n + close] + 1
                else:
                    # up to the next non-body char (anchor/footnote) or unit end
                    e = s
                    while e < u["end"] and (self.label[e] is None or self.src[e].isspace()
                                            or self.label[e][0] == "noise" or self.label[e][2] == body):
                        e += 1
                # only relabel body chars
                self.set_label(s, e, ("unit", i, role, None),
                               only_if=lambda L, i=i: L and L[0] == "unit" and L[1] == i and L[2] == body)

    # ------------------------------------------------------------ 7. noise
    def noise(self):
        cfg = self.cfg
        # running headers (main region only)
        for h in cfg["running_headers"]:
            for s, e in self.find_all(h, self.front_end, self.main_end):
                self.set_label(s, e, ("noise", "running_header"))
        # non-chosen heading occurrences (anywhere before back matter), when they look like headings
        for k, occs in enumerate(self.head_occs):
            for s, e in occs:
                if self.head_chosen[k] and (s, e) == self.head_chosen[k]:
                    continue
                if self.has_diacritics(s, e):
                    continue
                sa = self.standalone(s, e)
                if not sa and (len(arabic_words(self.src[s:e])) < 2 or not self.at_line_edge(s, e)):
                    continue
                if any(self.label[i] and self.label[i][0] == "unit" and self.label[i][2] == "heading" for i in range(s, e)):
                    continue
                self.set_label(s, e, ("noise", "displaced_heading"))
        for s, e, kind, ui in self.glued_digits:
            if kind == "number" and ui is not None:
                self.set_label(s, e, ("unit", ui, "number", None))
            else:
                self.set_label(s, e, ("noise", "page_number"))
        # printed page numbers glued to the first word of a line of main text ("٣٦لم يكن…")
        glued = re.compile(f"^\\s*([{DIGITS}]{{1,3}})(?=[\u0621-\u064A\\[«(])")
        for ls, le in self.lines:
            if not (self.main_start <= ls < self.main_end):
                continue
            m = glued.match(self.src[ls:le])
            if m:
                s = ls + m.start(1)
                lab = self.label[s]
                if lab and lab[0] == "unit" and lab[2] == cfg["body_role"] or lab and lab[0] == "node" and lab[1] == "main":
                    self.set_label(s, ls + m.end(1), ("noise", "page_number"))
        # separators (main region)
        sep = re.compile(cfg["separator_regex"])
        for ls, le in self.lines:
            if self.front_end <= ls < self.main_end and le > ls and sep.match(self.src[ls:le]):
                self.set_label(ls, le, ("noise", "separator"))
        # garbled glyphs
        g = cfg["garbled"]
        ranges = g["scripts"]
        for i, ch in enumerate(self.src):
            c = ord(ch)
            if any(a <= c <= b for a, b in ranges):
                self.set_label(i, i + 1, ("noise", "garbled_glyph"))
        skip_front = "front_matter" in g.get("skip_regions", [])
        for li, (ls, le) in enumerate(self.lines):
            if skip_front and ls < self.front_end:
                continue
            text = self.src[ls:le]
            for m in re.finditer(r"[A-Za-z]+", text):
                if len(m.group()) <= g["latin_max_run_in_arabic"]:
                    self.set_label(ls + m.start(), ls + m.end(), ("noise", "garbled_glyph"))

    # ------------------------------------------------------------ 8. overrides
    def apply_overrides(self):
        body = self.cfg["body_role"]
        for n, ov in enumerate(self.overrides):
            op = ov.get("op")
            if op == "set_unit_start":
                continue  # applied earlier
            if "reason" not in ov:
                self.issue("override_without_reason", "high", f"override #{n} has no reason", conf=1.0)
            if op == "mark_noise_range":
                a = self.resolve_at(ov["from"])
                b = self.resolve_at(ov["to"], a[0] if a else 0) if a else None
                if not a or not b:
                    self.issue("override_failed", "high", f"override #{n} (mark_noise_range): from/to not found", conf=1.0)
                    continue
                ov = dict(ov, at={"text": ov["from"]["text"]})
                at = (a[0], b[1])
                self.set_label(*at, ("noise", ov.get("type", "noise"), "override"))
                self.decisions.append((op, ov))
                self.issue("manual_decision", "low", f"{op}: {ov.get('reason', '')}",
                           [{"char_start": at[0], "char_end": at[1]}], conf=0.9)
                continue
            at = self.resolve_at(ov["at"]) if "at" in ov else None
            if "at" in ov and at is None:
                self.issue("override_failed", "high", f"override #{n} ({op}): text not found: {ov['at']}", conf=1.0)
                continue
            if op == "mark_noise":
                self.set_label(*at, ("noise", ov.get("type", "noise"), "override"))
            elif op == "assign":
                k = ov["to_ordinal"] - 1
                ui = next((i for i, u in enumerate(self.units) if u["k"] == k), None)
                role = ov.get("role", body)
                self.set_label(*at, ("unit", ui, role, ov.get("tag")))
            elif op == "set_role":
                s, e = at
                lab = self.label[s]
                if lab and lab[0] == "unit":
                    self.set_label(s, e, ("unit", lab[1], ov["role"], lab[3]))
            else:
                self.issue("override_failed", "high", f"override #{n}: unknown op {op}", conf=1.0)
                continue
            self.decisions.append((op, ov))
            host = self.unit_at(at[0])
            self.issue("manual_decision", "low", f"{op}: {ov.get('reason', '')}",
                       [{"unit_id": host, "char_start": at[0], "char_end": at[1]}] if host is not None
                       else [{"char_start": at[0], "char_end": at[1]}], conf=0.9)

    # ------------------------------------------------------------ 9. pages
    def page_model(self):
        pages = []
        if not self.pages_in:
            self.issue("page_boundary_unknown", "medium", "no page model supplied (--pages); page fields are null", conf=0.95)
            self.pages = []
            return
        markers = []
        for i in range(self.N):
            lab = self.label[i]
            if lab and lab[0] == "noise" and lab[1] == "running_header" and (i == 0 or self.label[i - 1] != lab):
                markers.append(("header", self.lines[self.line_of(i)][0]))
            if lab and lab[0] == "noise" and lab[1] == "page_number" and (i + 1 == self.N or self.label[i + 1] != lab):
                markers.append(("page_number", self.lines[self.line_of(i)][1] + 1))
        W = self.cfg["page_snap_window"]
        bounds = [p["char_start"] for p in self.pages_in]
        snapped = []
        for b in bounds[1:]:
            near = [(abs(pos - b), pos, kind) for kind, pos in markers if abs(pos - b) <= W]
            if near:
                d, pos, kind = min(near)
                snapped.append((pos, f"snapped_to_{kind}", 0.85))
            else:
                snapped.append((b, "pdf_alignment_only", min(0.6, self.pages_in[0].get("confidence", 0.6))))
        starts = [0] + [s[0] for s in snapped]
        for j in range(1, len(starts)):
            starts[j] = max(starts[j], starts[j - 1] + 1)
        for j, p in enumerate(self.pages_in):
            s = starts[j]
            e = starts[j + 1] if j + 1 < len(starts) else self.N
            ev = "document_start" if j == 0 else snapped[j - 1][1]
            cf = 0.9 if j == 0 else snapped[j - 1][2]
            pages.append({"page_index": p.get("page_index", j + 1), "printed_page_number": p.get("printed_page_number"),
                          "char_start": s, "char_end": e, "evidence": ev, "confidence": cf})
        self.pages = pages
        weak = [p for p in pages if p["evidence"] == "pdf_alignment_only"]
        if weak:
            self.issue("weak_page_boundary", "low",
                       f"{len(weak)} page boundaries could not be confirmed by a running header or page number",
                       [{"char_start": p["char_start"], "char_end": min(p["char_start"] + 1, p["char_end"])} for p in weak],
                       conf=0.7)

    def page_of(self, pos):
        for p in self.pages:
            if p["char_start"] <= pos < p["char_end"]:
                return p["page_index"]
        return None

    # ------------------------------------------------------------ 10. assemble
    def assemble(self):
        cfg = self.cfg
        src = self.src
        # runs of identical labels; whitespace is transparent
        runs = []
        cur, cs, ce = None, None, None
        for i, ch in enumerate(src):
            lab = self.label[i]
            if ch.isspace():
                continue
            if lab == cur and cur is not None:
                # merge across whitespace only
                if src[ce:i].strip() == "":
                    ce = i + 1
                    continue
            if cur is not None:
                runs.append((cur, cs, ce))
            cur, cs, ce = lab, i, i + 1
        if cur is not None:
            runs.append((cur, cs, ce))

        uid = lambda i: f"unit_{self.units[i]['ordinal']:03d}"
        # noise spans
        noise = []
        for lab, s, e in runs:
            if lab and lab[0] == "noise":
                z = {"id": f"noise_{len(noise)+1:04d}", "type": lab[1], "char_start": s, "char_end": e, "text": src[s:e]}
                if len(lab) > 2 and lab[2] == "override":
                    z["source"] = "override"
                noise.append(z)
        # unit segments
        segs = defaultdict(list)
        node_segs = defaultdict(list)
        for lab, s, e in runs:
            if lab is None:
                continue
            if lab[0] == "unit":
                d = {"role": lab[2], "char_start": s, "char_end": e}
                tag = lab[3]
                if tag and tag.startswith("fn"):
                    f = self.fn_list[int(tag[2:])]
                    d["footnote_id"] = f"fn_{f['fid']+1:03d}"
                    d["footnote_marker"] = f["marker_raw"]
                if tag and tag.startswith("an"):
                    a = self.an_list[int(tag[2:])]
                    d["marker"] = a["raw"]
                    if "fid" in a:
                        d["footnote_id"] = f"fn_{a['fid']+1:03d}"
                segs[lab[1]].append(d)
            elif lab[0] == "node":
                node_segs[lab[1]].append({"role": lab[2], "char_start": s, "char_end": e})

        # issues -> ids, attach units
        units_out = []
        foreign = defaultdict(list)
        for i, u in enumerate(self.units):
            for d in segs[i]:
                inside = u["start"] <= d["char_start"] and d["char_end"] <= u["end"]
                d["placement"] = "in_span" if inside else "displaced"
                if not inside:
                    host = self.unit_at(d["char_start"])
                    if host is not None:
                        foreign[host].append({"char_start": d["char_start"], "char_end": d["char_end"],
                                              "belongs_to": uid(i)})
                if self.pages:
                    d["page_index"] = self.page_of(d["char_start"])
        # confidence per role
        role_conf = {"heading": 0.95, "subtitle": 0.9, "number": 0.85, "anchor": 0.8, cfg["attribution"]["role"]: 0.85,
                     cfg["body_role"]: 0.85, "takhrij": 0.8, "gharib": 0.75}
        for i, u in enumerate(self.units):
            for d in segs[i]:
                d["confidence"] = role_conf.get(d["role"], 0.75)
                if "footnote_id" in d:
                    f = self.fn_list[int(d["footnote_id"][3:]) - 1]
                    if d["role"] != "anchor":
                        d["confidence"] = round(min(d["confidence"], f["conf"]), 2)

        # build issues with ids
        issues_out = []
        # group simple noise types into one issue each
        noise_groups = defaultdict(list)
        for z in noise:
            host = self.unit_at(z["char_start"])
            occ = {"char_start": z["char_start"], "char_end": z["char_end"]}
            if host is not None:
                occ["unit_id"] = host
            noise_groups[z["type"]].append(occ)
        noise_sev = {"running_header": "low", "page_number": "low", "separator": "low", "garbled_glyph": "low",
                     "displaced_heading": "low"}
        noise_desc = {
            "running_header": "Running header read into the text flow.",
            "page_number": "Printed page numbers read into the text flow.",
            "separator": "Stray separator lines.",
            "garbled_glyph": "Non-Arabic glyphs produced by OCR from ornaments or calligraphy.",
            "displaced_heading": "A unit heading read a second time at the wrong position (the real heading is kept on its unit).",
        }
        raw_issues = list(self.issues)
        for t, occ in noise_groups.items():
            raw_issues.append({"type": t if t != "page_number" else "page_number_noise", "severity": noise_sev.get(t, "low"),
                               "description": noise_desc.get(t, f"noise of type {t}"), "occurrences": occ, "confidence": 0.9})
        # heading-based issues
        for i, u in enumerate(self.units):
            if u["head_kind"] == "glued":
                raw_issues.append({"type": "glued_heading", "severity": "medium",
                                   "description": "heading found only glued to other text on its line",
                                   "occurrences": [{"unit_id": i, "char_start": u["head_span"][0], "char_end": u["head_span"][1]}],
                                   "confidence": 0.7})
            if u["head"].get("variants"):
                raw_issues.append({"type": "heading_toc_mismatch", "severity": "low",
                                   "description": f"body heading differs from TOC: {u['head']['variants']}",
                                   "occurrences": [{"unit_id": i, "char_start": u["head_span"][0], "char_end": u["head_span"][1]}],
                                   "confidence": 0.8, "needs_visual_check": True})
            if u["number_raw"] is None:
                pass  # ordinal is still known; not an issue by itself
        for c in cfg.get("expected_components", []):
            if c.get("status") != "present":
                raw_issues.append({"type": "possibly_missing_component", "severity": "medium",
                                   "description": f"{c['component']}: {c.get('note', 'not found in the source')}",
                                   "occurrences": [], "confidence": c.get("confidence", 0.6)})

        # trailing fragments: body text after the unit's attribution/footnotes (likely displaced from a neighbour)
        attr_role = cfg["attribution"]["role"]
        for i, u in enumerate(self.units):
            ins = [d for d in segs[i] if d["placement"] == "in_span"]
            last_attr = max((d["char_end"] for d in ins if d["role"] == attr_role), default=None)
            if last_attr is None:
                continue
            for d in ins:
                if d["role"] == cfg["body_role"] and d["char_start"] >= last_attr:
                    raw_issues.append({"type": "trailing_fragment", "severity": "low",
                                       "description": "text after the unit's attribution: second narration / author's comment / displaced fragment?",
                                       "occurrences": [{"unit_id": i, "char_start": d["char_start"], "char_end": d["char_end"]}],
                                       "confidence": 0.6, "needs_visual_check": True})

        # 1) resolve occurrence offsets to the final (trimmed) segments
        resolved = []
        for it in raw_issues:
            occs = []
            for o in it.get("occurrences", []):
                o = dict(o)
                ui = o.pop("unit_id", None)
                if "_fid" in it and ui is not None:
                    fsegs = [d for d in segs[ui] if d.get("footnote_id") == f"fn_{it['_fid']+1:03d}" and d["role"] != "anchor"]
                    if fsegs:
                        o["char_start"], o["char_end"] = fsegs[0]["char_start"], fsegs[0]["char_end"]
                if "_aid" in it and ui is not None:
                    pos = self.an_list[it["_aid"]]["s"]
                    asegs = [d for d in segs[ui] if d["role"] == "anchor" and d["char_start"] <= pos < d["char_end"]]
                    if asegs:
                        o["char_start"], o["char_end"] = asegs[0]["char_start"], asegs[0]["char_end"]
                s, e = o["char_start"], o["char_end"]
                while s < e and src[s].isspace():
                    s += 1
                while e > s and src[e - 1].isspace():
                    e -= 1
                if e <= s:
                    continue
                occ = {"char_start": s, "char_end": e}
                if ui is not None:
                    occ = {"unit_id": ui, **occ}
                if len(it.get("occurrences", [])) == 1 and it["type"] in GROUPED:
                    occ["note"] = it["description"]
                occs.append(occ)
            resolved.append(dict(it, occurrences=occs))

        # 2) group repeated patterns into one issue each (skill: Stage 9)
        grouped, order = {}, []
        for it in resolved:
            if it["type"] in GROUPED:
                g = grouped.get(it["type"])
                if g is None:
                    g = grouped[it["type"]] = {"type": it["type"], "severity": it["severity"],
                                               "description": GROUPED[it["type"]], "occurrences": [],
                                               "confidence": it["confidence"], "_confs": []}
                    order.append(g)
                g["occurrences"] += it["occurrences"]
                g["_confs"].append(it["confidence"])
                if it.get("needs_visual_check"):
                    g["needs_visual_check"] = True
            else:
                order.append(it)
        for g in grouped.values():
            g["confidence"] = round(sum(g["_confs"]) / len(g["_confs"]), 2)

        # 3) ids, unit links
        for n, it in enumerate(order):
            iid = f"issue_{n+1:03d}"
            occs = []
            for o in it["occurrences"]:
                o = dict(o)
                if "unit_id" in o:
                    self.units[o["unit_id"]]["issues"].add(iid)
                    o["unit_id"] = uid(o["unit_id"])
                occs.append(o)
            out = {"id": iid, "type": it["type"], "severity": it["severity"], "description": it["description"],
                   "occurrences": occs, "confidence": round(it["confidence"], 2)}
            for k in ("needs_visual_check", "candidates"):
                if k in it:
                    out[k] = it[k]
            issues_out.append(out)
        sev_of = {x["id"]: x["severity"] for x in issues_out}
        occ_units = {x["id"]: [o.get("unit_id") for o in x["occurrences"]] for x in issues_out}
        manual_ids = {x["id"] for x in issues_out if x["type"] == "manual_decision"}
        type_of = {x["id"]: x["type"] for x in issues_out}

        main_conf = 0.95
        for i, u in enumerate(self.units):
            s, e = u["start"], u["end"]
            while e > s and src[e - 1].isspace():
                e -= 1
            head_conf = {"standalone": 0.95, "glued": 0.8}.get(u["head_kind"], 0.85)
            conf = min(main_conf, head_conf)
            iids = sorted(u["issues"])
            has_disp = any(d["placement"] == "displaced" for d in segs[i]) or foreign[i]
            if has_disp:
                conf = min(conf, 0.8)   # displaced/foreign content caps the unit (skill Stage 8)
            # then a penalty per occurrence of other medium/high issues in this unit
            # (displaced_* issues are already reflected by the cap)
            for x in iids:
                if type_of[x].startswith("displaced_"):
                    continue
                n_occ = sum(1 for o in occ_units.get(x, []) if o == uid(i))
                conf -= {"medium": 0.05, "high": 0.15}.get(sev_of[x], 0) * max(1, n_occ)
                if type_of[x] == "trailing_fragment":
                    conf -= 0.03
            if any(x in manual_ids for x in iids):
                conf = min(conf, 0.85)
            conf = round(max(0.3, conf), 2)
            nrefs = [{"role": "noise_ref", "noise_id": z["id"]} for z in noise if s <= z["char_start"] < e]
            span = {"char_start": s, "char_end": e}
            if self.pages:
                span["page_start"] = self.page_of(s)
                span["page_end"] = self.page_of(e - 1)
            units_out.append({
                "id": uid(i), "type": cfg["unit_type"], "ordinal": u["ordinal"], "number_raw": u["number_raw"],
                "title": src[u["head_span"][0]:u["head_span"][1]], "title_variants": u["head"].get("variants", []),
                "parent_id": "node_002", "span": span, "raw_text": src[s:e],
                "segments": sorted(segs[i], key=lambda d: d["char_start"]) + nrefs,
                "foreign_spans": sorted(foreign[i], key=lambda d: d["char_start"]),
                "issue_ids": iids, "confidence": conf,
            })

        # nodes
        nodes = [{"id": "node_000", "type": "book", "title": cfg["metadata"].get("title"), "parent_id": None,
                  "children": ["node_001", "node_002"] + [f"node_{3+j:03d}" for j in range(len(self.back))],
                  "source": {"char_start": 0, "char_end": self.N}, "evidence": "whole source", "confidence": 0.95}]
        sec_id = {sc["key"]: f"node_s{sc['n']+1:03d}" for sc in self.sections}
        nodes.append({"id": "node_001", "type": "front_matter", "title": cfg["front_matter"].get("title"),
                      "parent_id": "node_000",
                      "children": [sec_id[sc["key"]] for sc in self.sections if sc["parent"] is None],
                      "source": {"char_start": 0, "char_end": self.front_end}, "evidence": "text before the main text",
                      "confidence": 0.9, "segments": node_segs["front"]})
        for sc in self.sections:
            nodes.append({"id": sec_id[sc["key"]], "type": sc["type"], "title": sc["title"], "level": sc["level"],
                          "parent_id": sec_id[sc["parent"]] if sc["parent"] else "node_001",
                          "children": [sec_id[c["key"]] for c in self.sections if c["parent"] == sc["key"]],
                          "source": {"char_start": sc["start"], "char_end": sc["end"]},
                          "evidence": "section heading from book_config" if sc["head"] else "section start from book_config (no heading line)",
                          "confidence": 0.9 if sc["head"] else 0.75, "segments": node_segs[sc["key"]]})
        main_node = {"id": "node_002", "type": cfg.get("main_type", "main_text"), "title": cfg.get("main_title"),
                     "parent_id": "node_000", "children": [x["id"] for x in units_out],
                     "source": {"char_start": self.main_start, "char_end": self.main_end},
                     "evidence": f"{len(units_out)} unit headings from book_config, located in order",
                     "confidence": main_conf}
        if node_segs.get("main"):
            main_node["segments"] = node_segs["main"]
        nodes.append(main_node)
        for j, b in enumerate(self.back):
            e = self.back[j + 1]["start"] if j + 1 < len(self.back) else self.N
            nodes.append({"id": f"node_{3+j:03d}", "type": b["type"], "title": b.get("title"), "parent_id": "node_000",
                          "children": [], "source": {"char_start": b["start"], "char_end": e},
                          "evidence": f"starts at '{b['start_at']['text']}'", "confidence": 0.8,
                          "segments": node_segs[f"back{j}"]})
        if self.pages:
            for n in nodes:
                n["source"]["page_start"] = self.page_of(n["source"]["char_start"])
                n["source"]["page_end"] = self.page_of(max(n["source"]["char_start"], n["source"]["char_end"] - 1))
        for z in noise:
            del z["text"]
        structure = {"root": "node_000", "pages": self.pages, "noise_spans": noise, "nodes": nodes}
        return structure, units_out, issues_out

    # ------------------------------------------------------------ run
    def build(self):
        self.find_regions()
        self.find_headings()
        self.base_labels()
        self.noise()            # first pass so numbers/footnotes see noise lines
        self.numbers()
        self.footnotes_and_anchors()
        self.attribution()
        self.noise()            # re-apply: noise wins over everything but overrides
        self.apply_overrides()
        self.page_model()
        return self.assemble()


# --------------------------------------------------------------------------- discover

def discover(src, cfg=None):
    lines = src.split("\n")
    cnt = Counter(l.strip() for l in lines if l.strip())
    repeated = [(t, c) for t, c in cnt.most_common(40) if c >= 3 and len(arabic_words(t)) >= 1
                and not re.match(DEFAULTS["standalone_number_regex"], t)]
    heads = []
    for i, l in enumerate(lines):
        t = l.strip()
        w = arabic_words(t)
        if 1 <= len(w) <= 8 and diacritic_ratio(t) < 0.05 and not re.search(r"[«»:\[\]]", t) \
                and not re.search(f"[{DIGITS}]", t) and cnt[t] < 5:
            nxt = next((lines[j] for j in range(i + 1, min(i + 4, len(lines))) if arabic_words(lines[j])), "")
            heads.append({"line": i + 1, "text": t, "next_line_diacritic_ratio": round(diacritic_ratio(nxt), 2)})
    fn = sum(1 for l in lines if re.match(r"^\s*\(\s*[0-9٠-٩۰-۹]{1,2}\s*\)\s*\S", l))
    anchors = sum(len(re.findall(r"\(\s*[0-9٠-٩۰-۹]{1,2}\s*\)", l)) for l in lines)
    ratios = [diacritic_ratio(l) for l in lines if len(arabic_words(l)) >= 4]
    out = {
        "lines": len(lines), "chars": len(src),
        "repeated_lines (running header candidates)": repeated,
        "footnote_start_lines": fn, "marker_occurrences": anchors,
        "diacritic_ratio_quartiles": sorted(ratios)[len(ratios) // 4::len(ratios) // 4][:3] if ratios else [],
        "heading_candidates (short, undiacritized lines; strong if next line is diacritized)":
            [h for h in heads if h["next_line_diacritic_ratio"] > 0.3][:200],
        "weaker_heading_candidates": [h for h in heads if h["next_line_diacritic_ratio"] <= 0.3][:120],
    }
    if cfg:
        missing = []
        nt = NormText(src)
        for h in cfg["unit_headings"]:
            if norm(h["text"]) not in nt.text:
                missing.append(h["text"])
        out["config_headings_not_found"] = missing
    return out


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("discover")
    d.add_argument("--source", required=True)
    d.add_argument("--config")
    b = sub.add_parser("build")
    b.add_argument("--source", required=True)
    b.add_argument("--config", required=True)
    b.add_argument("--overrides")
    b.add_argument("--pages")
    b.add_argument("--out", required=True)
    args = ap.parse_args()

    src_bytes = open(args.source, "rb").read()
    src = src_bytes.decode("utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    if args.cmd == "discover":
        cfg = load_config(args.config) if args.config else None
        print(json.dumps(discover(src, cfg), ensure_ascii=False, indent=1))
        return

    cfg = load_config(args.config)
    overrides = json.load(open(args.overrides, encoding="utf-8")).get("overrides", []) if args.overrides else []
    pages = []
    if args.pages:
        p = json.load(open(args.pages, encoding="utf-8"))
        pages = p["pages"] if isinstance(p, dict) else p
    eng = Engine(src, cfg, overrides, pages)
    structure, units, issues = eng.build()

    meta = cfg["metadata"]
    profile = {
        "title": meta.get("title"), "author": meta.get("author"),
        "editor_commentator": meta.get("editor_commentator"), "publisher": meta.get("publisher"),
        "edition": meta.get("edition"), "publication_year": meta.get("publication_year"),
        "isbn": meta.get("isbn"), "genre": meta.get("genre"), "language": meta.get("language", "ar"),
        "source_file": os.path.basename(args.source),
        "source_sha256": hashlib.sha256(src_bytes).hexdigest(),
        "source_length_chars": len(src),
        "line_endings": "CRLF" if "\r\n" in src else "LF",
        "sources_used": [os.path.basename(args.source)] + ([os.path.basename(args.pages) + " (page model)"] if args.pages else []),
        "generated_by": "book-structure-analyzer/scripts/engine.py",
        "config_file": os.path.basename(args.config),
        "overrides_file": os.path.basename(args.overrides) if args.overrides else None,
        "overrides_applied": len(eng.decisions),
        "expected_components": cfg.get("expected_components", []),
        "segment_roles_used": sorted({s["role"] for u in units for s in u["segments"]}),
        "structure_summary": meta.get("structure_summary"),
        "notes": meta.get("notes", []),
    }
    os.makedirs(args.out, exist_ok=True)
    for name, obj in (("book_profile.json", profile), ("structure.json", structure),
                      ("units.json", {"units": units}), ("issues.json", {"issues": issues})):
        with open(os.path.join(args.out, name), "w", encoding="utf-8", newline="\n") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.write("\n")
    roles = Counter(s["role"] for u in units for s in u["segments"])
    print(f"units: {len(units)}  issues: {len(issues)}  noise: {len(structure['noise_spans'])}  "
          f"pages: {len(structure['pages'])}  overrides applied: {len(eng.decisions)}")
    print("roles:", dict(roles))
    print("next: python validate.py --source", args.source, "--out", args.out)


if __name__ == "__main__":
    main()
