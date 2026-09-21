#!/usr/bin/env python3
"""extract_pages.py - derive the page model for the-40-ocr-fastocr.txt from the
searchable PDF (C:\\Users\\Islam\\Downloads\\the-40-searchable-fastocr.pdf).

The fastocr searchable PDF carries a text layer whose word order is scrambled
(RTL extraction), so exact substring matching fails.  We align each PDF page to
the TXT by fuzzy 5-gram position matching (diacritics and whitespace stripped),
then enforce monotone page order and interpolate pages that cannot be anchored.

Output: <out>/pages.json  (list of {page_index, printed_page_number, char_start,
char_end, evidence, confidence}); char offsets are Unicode code-point indices
into the TXT (UTF-8, LF as on disk, char_end exclusive).
"""
import json
import re
import sys

TXT = r"E:\موقع التراث\AI-Project\the-40-ocr-fastocr.txt"
PDF_TEXT = r"C:\Users\Islam\AppData\Local\Temp\opencode\searchable_pages.json"
OUT = r"E:\موقع التراث\AI-Project\builder\pages.json"

G = 5          # n-gram size
SAMPLE = 3     # sample step inside each page's packed text
MIN_HITS = 12  # a page with fewer raw hits cannot be anchored reliably
PAGE_END_FALLBACK = 140  # per-page packed-length used for interpolation


def strip_diacritics(s):
    s = re.sub(r"[\u064B-\u0652\u0670\u0640\u06D6-\u06ED]", "", s)
    return s.replace("\u200f", "").replace("\u200e", "").replace("\u200c", "")


def norm(s):
    return re.sub(r"\s+", "", strip_diacritics(s))


def main():
    txt = open(TXT, encoding="utf-8").read()
    pages = json.load(open(PDF_TEXT, encoding="utf-8"))
    n_pages = len(pages)

    # packed TXT + mapping pack-index -> source char offset
    pack = ""
    pack_to_src = []
    for i, ch in enumerate(txt):
        c = norm(ch)
        if c:
            pack += c
            pack_to_src.append(i)
    N_pack = len(pack)
    pack_to_src.append(len(txt))  # sentinel

    # 5-gram index
    idx = {}
    for start in range(N_pack - G + 1):
        idx.setdefault(pack[start:start + G], []).append(start)

    # ---- per-page hit clusters (pack positions) ---------------------------
    def clusters(page_text):
        np_ = norm(page_text)
        hits = {}
        for start in range(0, max(0, len(np_) - G + 1), SAMPLE):
            g = np_[start:start + G]
            for p in idx.get(g, []):
                hits[p] = hits.get(p, 0) + 1
        pos = sorted(hits)
        if len(pos) < 3:
            return None
        # collect local clusters: contiguous runs of positions with a hit
        runs = []
        run = []
        prev = None
        for p in pos:
            if prev is not None and p - prev > G:
                if len(run) >= 3:
                    runs.append(run)
                run = []
            run.append(p)
            prev = p
        if len(run) >= 3:
            runs.append(run)
        out = []
        for r in runs:
            lo, hi = r[0], r[-1]
            score = sum(hits[p] for p in r) / max(1, hi - lo)
            w = sum(hits[p] for p in r)
            out.append((lo, hi, w, score))
        out.sort(key=lambda t: -t[2])
        return out

    page_clusters = []
    for pg in pages:
        page_clusters.append(clusters(pg["text"]))

    # ---- monotone forward chaining ---------------------------------------
    # choose the first cluster for each page that is strictly forward of the
    # previous page's chosen cluster start, preferring the densest cluster.
    chosen = []  # (lo, hi) in pack space or None
    lower = 0
    for i in range(n_pages):
        cls = page_clusters[i]
        pick = None
        if cls:
            forward = [c for c in cls if c[0] >= lower]
            if forward:
                pick = forward[0]
                lower = pick[1] + 1
            else:
                # no forward cluster: fall back to the most dense (may equal)
                pick = cls[0]
                lower = max(lower, pick[1] + 1)
        chosen.append(pick)

    # ---- boundaries in pack space ----------------------------------------
    # page i occupies [b[i], b[i+1]); for the last page b[n] = its cluster hi.
    b = [None] * (n_pages + 1)
    b[0] = 0
    last_ppy = None  # last reliably anchored page index
    last_ph = None   # its pack hi (end)
    for i in range(n_pages):
        c = chosen[i]
        if c is not None:
            b[i] = c[0]
            last_ppy = i
            last_ph = c[1]
    # the final boundary is the end of the document: the last page (back-matter
    # index end) coincides with the end of the source.
    b[n_pages] = N_pack

    # fill any remaining None boundaries (pages placed after a None) by
    # interpolation in pack space between the previous known boundary and the
    # next known boundary.
    i = 1
    while i <= n_pages:
        if b[i] is not None:
            i += 1
            continue
        # find next known boundary
        j = i
        while j <= n_pages and b[j] is None:
            j += 1
        if j > n_pages:
            # tail unknown: extrapolate by fixed size from previous boundary
            prev_b = b[i - 1]
            if prev_b is None:
                prev_b = N_pack
            for k in range(i, n_pages + 1):
                b[k] = prev_b + PAGE_END_FALLBACK * (k - i + 1)
            b[n_pages] = N_pack
            break
        seg_pages = j - i + 1
        seg_span = b[j] - b[i - 1]
        for k in range(i, j):
            b[k] = b[i - 1] + int(round(seg_span * (k - i + 1) / seg_pages))
        i = j + 1

    # enforce monotonic
    for k in range(1, n_pages + 1):
        if b[k] is not None and b[k - 1] is not None and b[k] < b[k - 1]:
            b[k] = b[k - 1]

    # ---- emit pages --------------------------------------------------------
    pages_out = []
    for i in range(1, n_pages + 1):
        s_pack = b[i - 1]
        e_pack = b[i]
        s_pack = max(0, s_pack)
        e_pack = min(N_pack, max(s_pack + 1, e_pack))
        cs = pack_to_src[s_pack]
        ce = pack_to_src[e_pack]
        anchored = chosen[i - 1] is not None
        if anchored:
            confidence = 0.9
            evidence = "searchable_pdf_text_layer_alignment"
        else:
            confidence = 0.6
            evidence = "interpolated_between_aligned_pages"
        pages_out.append({
            "page_index": i,
            "printed_page_number": None,
            "char_start": cs,
            "char_end": ce,
            "evidence": evidence,
            "confidence": confidence,
        })

    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        json.dump(pages_out, f, ensure_ascii=False, indent=2)
        f.write("\n")

    # ---- diagnostic ---------------------------------------------------------
    sys.stdout.reconfigure(encoding="utf-8")
    for p in pages_out:
        lo_line = txt[:p["char_start"]].count("\n") + 1
        hi_line = txt[:p["char_end"]].count("\n") + 1
        print(f"page {p['page_index']:>2}  src [{p['char_start']:>6},{p['char_end']:>6})  "
              f"lines {lo_line:>3}..{hi_line:>3}  {p['evidence']}  conf {p['confidence']}")
    print("pages:", len(pages_out), "written to", OUT)


if __name__ == "__main__":
    main()