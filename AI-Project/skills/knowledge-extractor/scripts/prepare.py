#!/usr/bin/env python3
"""
prepare.py — make one reading sheet per unit for the knowledge extractor.

Input: the approved output of book-structure-analyzer (units.json + structure.json) and the source
text. Output, per unit, in --out:
  <unit_id>.md    the sheet the extractor reads: the unit's text in numbered pieces, each piece
                  labelled with whose words it is (author / editor)
  <unit_id>.json  the same pieces with the source ranges they came from (used by check.py)

Pieces of a unit:
  heading       the unit's heading (+ subtitle) — author's or editor's words, per the config
  main          all of the unit's main text, in text order (anchors and noise removed)
  attribution   the author's source attribution ([رواه مسلم] …)
  footnote      one piece per editor's footnote part (by footnote_id and role), for the roles the
                config includes

Usage:
  python prepare.py --source book.txt --build <structure build dir> --config extraction_config.json \
                    --out sheets/ [--units unit_002,unit_004]
"""

import argparse
import os
import re
import sys

from common import load_json, write_json

NON_BODY = {"heading", "subtitle", "number", "anchor", "source_attribution", "takhrij", "gharib",
            "footnote", "noise_ref"}
FOOTNOTE_ROLES = {"takhrij", "gharib", "footnote"}
JOIN = " "   # between segments of one piece


def build_pieces(u, src, cfg):
    segs = sorted((g for g in u["segments"] if "char_start" in g), key=lambda g: g["char_start"])
    groups, order = {}, []

    def add(key, kind, by, label, g):
        if key not in groups:
            groups[key] = {"kind": kind, "by": by, "label": label, "segments": []}
            order.append(key)
        groups[key]["segments"].append(g)

    fn_cfg = cfg.get("footnote_roles", {})
    for g in segs:
        r = g["role"]
        if r in ("heading", "subtitle"):
            add("heading", "heading", cfg.get("heading_by", "editor"), "heading", g)
        elif r == "source_attribution":
            add("attribution", "attribution", "author", "source attribution", g)
        elif r in FOOTNOTE_ROLES:
            if fn_cfg.get(r, "exclude") != "include":
                continue
            fid = g.get("footnote_id", "?")
            m = re.match(r"\s*(\([^)]{1,4}\))", g.get("footnote_marker") or "")
            add(("fn", fid, r), "footnote", "editor", f"footnote {m.group(1) if m else ''} ({r})".replace("  ", " "), g)
        elif r not in NON_BODY:
            add("main", "main", cfg.get("main_text_by", "author"), "main text", g)

    rank = {"heading": 0, "main": 1, "attribution": 2}
    order.sort(key=lambda k: (rank.get(k, 3), groups[k]["segments"][0]["char_start"]))
    pieces = []
    for n, k in enumerate(order, 1):
        grp = groups[k]
        text, spans = "", []
        for g in grp["segments"]:
            if text:
                text += JOIN
            part = src[g["char_start"]:g["char_end"]].replace("\n", " ")
            spans.append({"piece_start": len(text), "char_start": g["char_start"], "char_end": g["char_end"],
                          "role": g["role"], "page": g.get("page_index")})
            text += part
        pieces.append({"id": f"p{n:02d}", "kind": grp["kind"], "by": grp["by"], "label": grp["label"],
                       "text": text, "spans": spans})
    return pieces


def sheet_md(u, pieces):
    lines = [f"UNIT {u['id']}  ({u['type']} {u.get('ordinal')})", ""]
    for p in pieces:
        who = "the AUTHOR's text" if p["by"] == "author" else "the EDITOR's words"
        if p["kind"] == "main":
            who += " (it may report the words of others: the Prophet ﷺ, a verse, a quoted person)"
        lines.append(f"[{p['id']}] {p['label']} — {who}")
        lines.append(p["text"].strip())
        lines.append("")
    return "\n".join(lines)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True)
    ap.add_argument("--build", required=True, help="book-structure-analyzer output folder (units.json, structure.json)")
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--units", default="all", help="comma-separated unit ids, or 'all'")
    a = ap.parse_args()

    src = open(a.source, encoding="utf-8").read()
    cfg = load_json(a.config)
    units = load_json(os.path.join(a.build, "units.json"))["units"]
    want = None if a.units == "all" else set(a.units.split(","))
    os.makedirs(a.out, exist_ok=True)
    n = 0
    for u in units:
        if want is not None and u["id"] not in want:
            continue
        pieces = build_pieces(u, src, cfg)
        write_json(os.path.join(a.out, f"{u['id']}.json"),
                   {"unit_id": u["id"], "type": u["type"], "ordinal": u.get("ordinal"),
                    "confidence": u.get("confidence"), "pieces": pieces})
        with open(os.path.join(a.out, f"{u['id']}.md"), "w", encoding="utf-8", newline="\n") as f:
            f.write(sheet_md(u, pieces))
        n += 1
    if want is not None and n != len(want):
        missing = want - {u["id"] for u in units}
        sys.exit(f"unknown unit ids: {', '.join(sorted(missing))}")
    print(f"{n} sheet(s) written to {a.out}")


if __name__ == "__main__":
    main()
