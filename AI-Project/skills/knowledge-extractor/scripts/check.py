#!/usr/bin/env python3
"""
check.py — check the extractor's drafts, resolve every quote to exact source offsets, and write
knowledge_objects.json. Nothing is accepted that cannot be traced to the source.

A draft (drafts/<unit_id>.json, written by the extractor from the unit's sheet) is:
  {"unit_id": ..., "model_used": ..., "prompt_version": ..., "no_knowledge_reason": null | "...",
   "objects": [{"type", "statement", "said_by": {"kind", "name_in_text"?, "reported_by"?},
                "basis": "explicit" | "inferred", "inference_reason"?, "evidence": [{"piece", "quote"}]}]}

An object is REJECTED (with a reason code) when:
  E_TYPE              type not in the book's extraction_config.json
  E_STATEMENT         statement empty, not Arabic, or longer than max_statement_chars
  E_BASIS             basis not explicit/inferred, or inferred without inference_reason
  E_NO_EVIDENCE       no evidence
  E_PIECE             evidence names a piece that is not in the unit's sheet
  E_QUOTE_NOT_FOUND   the quote is not in that piece word for word (diacritics, letter variants,
                      punctuation and spacing are ignored; nothing else is)
  E_QUOTE_SHORT       the quote has fewer than min_quote_letters letters
  E_SPEAKER_KIND      said_by.kind not allowed by the config
  E_SPEAKER_ROLE      said_by does not fit whose words the evidence is: editor ⇔ editor's pieces only;
                      author / prophet / quran / quoted ⇔ author's pieces only
  E_NAME_NOT_IN_TEXT  prophet / quoted without a name_in_text found in the unit's author text
A DRAFT is rejected when it is unreadable (E_JSON), for another unit (E_UNIT), for another prompt
version (E_PROMPT_VERSION), without model_used (E_MODEL), without objects and without
no_knowledge_reason (E_EMPTY), or with more than max_objects_per_unit objects (E_TOO_MANY).
A requested unit without a draft is E_MISSING_DRAFT.

Confidence is computed, never taken from the draft: explicit 0.85 / inferred 0.6, capped by the
unit's structure confidence.

Usage:
  python check.py --sheets sheets/ --drafts drafts/ --config extraction_config.json \
                  --out knowledge_objects.json [--units unit_002,unit_004]
Exit code 0 = every requested unit accepted in full; 1 = rejections or missing drafts (see report).
"""

import argparse
import json
import os
import sys

from common import (AUTHOR_VOICE, EDITOR_VOICE, NAME_REQUIRED, letters, load_json, norm, normalize,
                    write_json)

PROMPT_VERSION = "ke-v1"
DEFAULTS = {"min_quote_letters": 8, "max_statement_chars": 300, "max_objects_per_unit": 8,
            "speaker_kinds": ["author", "prophet", "quran", "quoted", "editor"],
            "confidence": {"explicit": 0.85, "inferred": 0.6}}


def find_quote(piece, quote):
    """source location of quote inside piece, or None: the overall range, and the exact parts (one
    per source segment it touches — a quote across a page break skips what lies between)"""
    ptext_n, pidx = normalize(piece["text"])
    q = norm(quote)
    if not q:
        return None
    at = ptext_n.find(q)
    if at < 0:
        return None
    ps, pe = pidx[at], pidx[at + len(q) - 1] + 1          # piece positions, end exclusive
    parts = []
    for sp in piece["spans"]:
        a = sp["piece_start"]
        b = a + (sp["char_end"] - sp["char_start"])
        lo, hi = max(ps, a), min(pe, b)
        if lo < hi:
            parts.append({"char_start": sp["char_start"] + lo - a, "char_end": sp["char_start"] + hi - a,
                          "role": sp["role"], "page": sp.get("page")})
    if not parts:
        return None
    return {"char_start": parts[0]["char_start"], "char_end": parts[-1]["char_end"], "role": parts[0]["role"],
            "page": parts[0]["page"], "page_end": parts[-1]["page"], "parts": parts}


def check_object(o, pieces, cfg, author_text_n):
    reasons = []
    types = {t["id"] for t in cfg["types"]}
    if o.get("type") not in types:
        reasons.append(f"E_TYPE: '{o.get('type')}' is not one of {sorted(types)}")
    st = o.get("statement") or ""
    if not st.strip() or not letters(st) or len(st) > cfg["max_statement_chars"]:
        reasons.append("E_STATEMENT: empty, not Arabic, or longer than %d characters" % cfg["max_statement_chars"])
    basis = o.get("basis")
    if basis not in ("explicit", "inferred") or (basis == "inferred" and not (o.get("inference_reason") or "").strip()):
        reasons.append("E_BASIS: basis must be 'explicit', or 'inferred' with an inference_reason")
    sb = o.get("said_by") or {}
    kind = sb.get("kind")
    if kind not in cfg["speaker_kinds"]:
        reasons.append(f"E_SPEAKER_KIND: '{kind}' is not one of {cfg['speaker_kinds']}")
    ev_out = []
    evs = o.get("evidence") or []
    if not evs:
        reasons.append("E_NO_EVIDENCE: at least one quote is required")
    for k, ev in enumerate(evs):
        p = pieces.get(ev.get("piece"))
        if p is None:
            reasons.append(f"E_PIECE: evidence[{k}] names piece '{ev.get('piece')}', not in this unit")
            continue
        q = ev.get("quote") or ""
        if letters(q) < cfg["min_quote_letters"]:
            reasons.append(f"E_QUOTE_SHORT: evidence[{k}] quote has fewer than {cfg['min_quote_letters']} letters")
            continue
        loc = find_quote(p, q)
        if loc is None:
            reasons.append(f"E_QUOTE_NOT_FOUND: evidence[{k}] quote is not in {p['id']} word for word: «{q[:60]}»")
            continue
        if kind in EDITOR_VOICE and p["by"] != "editor":
            reasons.append(f"E_SPEAKER_ROLE: said_by '{kind}' but evidence[{k}] is in {p['id']}, the author's text")
        if kind in AUTHOR_VOICE and p["by"] != "author":
            reasons.append(f"E_SPEAKER_ROLE: said_by '{kind}' but evidence[{k}] is in {p['id']}, the editor's words")
        ev_out.append({"piece": p["id"], "quote": q, **loc})
    if kind in NAME_REQUIRED:
        name = norm(sb.get("name_in_text") or "")
        if not name or name not in author_text_n:
            reasons.append(f"E_NAME_NOT_IN_TEXT: said_by '{kind}' needs a name_in_text found in the unit's author text "
                           f"(got «{sb.get('name_in_text')}»)")
    return reasons, ev_out


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sheets", required=True)
    ap.add_argument("--drafts", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--units", default="all", help="comma-separated unit ids, or 'all' (every sheet)")
    a = ap.parse_args()

    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in load_json(a.config).items() if not k.startswith("_")})
    sheet_ids = sorted(f[:-5] for f in os.listdir(a.sheets) if f.endswith(".json"))
    want = sheet_ids if a.units == "all" else a.units.split(",")

    accepted, rejected, per_unit = [], [], {}
    for uid in want:
        sheet_path = os.path.join(a.sheets, f"{uid}.json")
        if not os.path.exists(sheet_path):
            rejected.append({"unit_id": uid, "object": None, "reasons": ["E_NO_SHEET: run prepare.py for this unit"]})
            continue
        sheet = load_json(sheet_path)
        pieces = {p["id"]: p for p in sheet["pieces"]}
        author_text_n = " ".join(norm(p["text"]) for p in sheet["pieces"] if p["by"] == "author")
        dpath = os.path.join(a.drafts, f"{uid}.json")
        info = {"accepted": 0, "rejected": 0, "no_knowledge_reason": None}
        per_unit[uid] = info
        if not os.path.exists(dpath):
            rejected.append({"unit_id": uid, "object": None, "reasons": ["E_MISSING_DRAFT: no drafts/%s.json" % uid]})
            continue
        try:
            d = load_json(dpath)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            rejected.append({"unit_id": uid, "object": None, "reasons": [f"E_JSON: {e}"]})
            continue
        draft_errors = []
        if d.get("unit_id") != uid:
            draft_errors.append(f"E_UNIT: draft says unit_id '{d.get('unit_id')}'")
        if d.get("prompt_version") != PROMPT_VERSION:
            draft_errors.append(f"E_PROMPT_VERSION: expected '{PROMPT_VERSION}', got '{d.get('prompt_version')}'")
        if not (d.get("model_used") or "").strip():
            draft_errors.append("E_MODEL: model_used is empty")
        objs = d.get("objects") or []
        if not objs and not (d.get("no_knowledge_reason") or "").strip():
            draft_errors.append("E_EMPTY: no objects and no no_knowledge_reason")
        if len(objs) > cfg["max_objects_per_unit"]:
            draft_errors.append(f"E_TOO_MANY: {len(objs)} objects (max {cfg['max_objects_per_unit']})")
        if draft_errors:
            rejected.append({"unit_id": uid, "object": None, "reasons": draft_errors})
            info["rejected"] = len(objs) or 1
            continue
        info["no_knowledge_reason"] = d.get("no_knowledge_reason")
        seen = set()
        for i, o in enumerate(objs):
            reasons, evs = check_object(o, pieces, cfg, author_text_n)
            if reasons:
                rejected.append({"unit_id": uid, "object": i, "statement": o.get("statement"), "reasons": reasons})
                info["rejected"] += 1
                continue
            key = norm(o["statement"])
            warn = ["W_DUPLICATE: same statement as another object of this unit"] if key in seen else []
            seen.add(key)
            base = cfg["confidence"][o["basis"]]
            conf = round(min(base, sheet.get("confidence") or base), 2)
            info["accepted"] += 1
            ko = {"id": f"ko_{uid}_{info['accepted']:02d}", "unit_id": uid, "type": o["type"],
                  "statement": o["statement"].strip(), "said_by": o["said_by"], "basis": o["basis"]}
            if o["basis"] == "inferred":
                ko["inference_reason"] = o["inference_reason"]
            ko.update({"evidence": evs, "confidence": conf, "model_used": d["model_used"],
                       "prompt_version": PROMPT_VERSION, "status": "experimental"})
            if warn:
                ko["warnings"] = warn
            accepted.append(ko)

    out = {"prompt_version": PROMPT_VERSION, "units_checked": want, "per_unit": per_unit,
           "objects": accepted, "rejected": rejected}
    write_json(a.out, out)
    status = "pass" if not rejected else "fail"
    print(f"status: {status}  units: {len(want)}  accepted: {len(accepted)}  rejected: {len(rejected)}  -> {a.out}")
    for r in rejected:
        where = f"{r['unit_id']}" + (f" object {r['object']}" if r.get("object") is not None else "")
        for reason in r["reasons"]:
            print(f"  REJECTED {where}: {reason}")
    sys.exit(0 if status == "pass" else 1)


if __name__ == "__main__":
    main()
