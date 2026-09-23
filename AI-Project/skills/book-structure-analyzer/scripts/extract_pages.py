#!/usr/bin/env python3
"""
extract_pages.py — build a page model (pages.json) for an OCR text file from the
searchable PDF produced by the same OCR run.

Searchable-PDF text layers are often scrambled (right-to-left extraction reorders words),
so pages are aligned to the TXT by fuzzy n-gram matching on diacritic-free, whitespace-free
text, with page order enforced. Pages that cannot be matched are interpolated and get lower
confidence. engine.py later snaps these boundaries to running headers / page numbers found
in the TXT, and marks the ones it could not confirm.

Line layout (PyMuPDF only): every PDF text line is kept with its bounding box, and every TXT
line is matched to the PDF line it came from (letter-bigram containment, searched on the
aligned page and its neighbours). engine.py uses these positions to tell the footnote zone
at the bottom of a page from the main text — the information plain OCR text loses.
Output: {"pages": [...each with "height" and "lines"...], "txt_lines": [...]}.

Usage:
  python extract_pages.py --txt book.txt --pdf book-searchable.pdf --out pages.json
  python extract_pages.py --txt book.txt --pdf-text-json pages_text.json --out pages.json
      (pages_text.json = [{"text": "...", "height"?: h, "lines"?: [{"bbox": [x0,y0,x1,y1], "text": "..."}]}, ...]
       one entry per PDF page, in order)

Needs PyMuPDF (pip install pymupdf) or pypdf (pip install pypdf, no line layout) for --pdf.
"""
import argparse
import json
import re
import sys
from collections import Counter

G = 5            # n-gram size
SAMPLE = 3       # sampling step inside each page's packed text
MIN_WEIGHT = 12  # a page whose best cluster has fewer n-gram hits is treated as unmatched


def strip_marks(s):
    s = re.sub(r"[\u064B-\u0652\u0670\u0640\u06D6-\u06ED]", "", s)
    return re.sub(r"[\u200c-\u200f]", "", s)


def norm(s):
    return re.sub(r"\s+", "", strip_marks(s))


def pdf_pages(path):
    try:
        try:
            import pymupdf as fitz
        except ImportError:
            import fitz  # PyMuPDF, older name
        doc = fitz.open(path)
        out = []
        for p in doc:
            lines = []
            for b in p.get_text("dict")["blocks"]:
                for ln in b.get("lines", []):
                    t = "".join(s["text"] for s in ln["spans"])
                    if t.strip():
                        lines.append({"bbox": [round(v, 1) for v in ln["bbox"]], "text": t})
            lines.sort(key=lambda l: (l["bbox"][1], -l["bbox"][2]))
            out.append({"text": p.get_text(), "height": round(p.rect.height, 1), "lines": lines})
        return out
    except ImportError:
        pass
    try:
        from pypdf import PdfReader
        return [{"text": p.extract_text() or ""} for p in PdfReader(path).pages]
    except ImportError:
        sys.exit("install PyMuPDF (pip install pymupdf) or pypdf (pip install pypdf), or pass --pdf-text-json")


def align(txt, pages):
    pack, pack_to_src = [], []
    for i, ch in enumerate(txt):
        c = norm(ch)
        if c:
            pack.append(c)
            pack_to_src.append(i)
    pack = "".join(pack)
    n_pack = len(pack)
    pack_to_src.append(len(txt))
    idx = {}
    for s in range(n_pack - G + 1):
        idx.setdefault(pack[s:s + G], []).append(s)

    def best_window(text, lower):
        """window of the page's own length that contains the most n-gram hits (order-free),
        starting at or after `lower`; returns (start, weight)"""
        t = norm(text)
        W = max(G, int(len(t) * 1.1))
        hits = {}
        for s in range(0, max(0, len(t) - G + 1), SAMPLE):
            for p in idx.get(t[s:s + G], []):
                if p >= lower:
                    hits[p] = hits.get(p, 0) + 1
        pos = sorted(hits)
        best, best_w, j, cur = None, 0, 0, 0
        for i, p in enumerate(pos):
            while j < len(pos) and pos[j] < p + W:
                cur += hits[pos[j]]
                j += 1
            if cur > best_w:
                best, best_w = p, cur
            cur -= hits[p]
        return best, best_w

    n = len(pages)
    starts = [None] * n
    lower = 0
    for i, pg in enumerate(pages):
        s, w = best_window(pg["text"], lower)
        if s is not None and w >= MIN_WEIGHT:
            starts[i] = s
            lower = s + 1
    starts[0] = 0
    known = [i for i in range(n) if starts[i] is not None]
    for a, b in zip(known, known[1:] + [n]):
        end = starts[b] if b < n else n_pack
        for k in range(a + 1, b):
            starts[k] = starts[a] + (end - starts[a]) * (k - a) // (b - a)
    for k in range(1, n):
        starts[k] = max(starts[k], starts[k - 1] + 1)
    out = []
    for i in range(n):
        s = pack_to_src[min(starts[i], n_pack)]
        e = pack_to_src[min(starts[i + 1], n_pack)] if i + 1 < n else len(txt)
        matched = i in known
        out.append({"page_index": i + 1, "printed_page_number": None, "char_start": s, "char_end": max(e, s + 1),
                    "evidence": "pdf_text_alignment" if matched else "interpolated",
                    "confidence": 0.75 if matched else 0.5})
    return out


LETTER_MAP = {"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ة": "ه", "ی": "ي", "ک": "ك"}
DIGIT_MAP = {c: str(i) for s in ("٠١٢٣٤٥٦٧٨٩", "۰۱۲۳۴۵۶۷۸۹", "0123456789") for i, c in enumerate(s)}


def tokens(s):
    """Order-free fingerprint of a line: (letters, digits). Letters = bigrams inside each word (the PDF
    text layer may reverse word order, never the letters inside a word), a single-letter word as itself.
    Digits are kept apart: they decide between equally good letter matches, but a page number that OCR
    glued to a line must not lower its score."""
    out, dig = Counter(), Counter()
    for w in re.findall(r"[ء-يٱ-ۓ]+", strip_marks(s)):
        w = "".join(LETTER_MAP.get(c, c) for c in w)
        if len(w) == 1:
            out["1" + w] += 1
        for i in range(len(w) - 1):
            out[w[i:i + 2]] += 1
    for c in s:
        if c in DIGIT_MAP:
            dig["#" + DIGIT_MAP[c]] += 1
    return out, dig


def match_lines(txt, pages, res):
    """Match every TXT line to the PDF line it was read from.
    Returns [{char_start, char_end, page_index, line, score, cover, n}] (page_index/line null when unmatched):
    score = share of the TXT line's tokens found in the PDF line, cover = share of the PDF line explained by it."""
    fps = [[tokens(l["text"]) for l in pg.get("lines", [])] for pg in pages]
    starts = [p["char_start"] for p in res]
    out, pos = [], 0
    prev = (None, -1.0)   # (page, y0) of the previous matched line: ties go to the next line in reading order
    for ln in txt.split("\n"):
        s, e = pos, pos + len(ln)
        pos = e + 1
        rec = {"char_start": s, "char_end": e, "page_index": None, "line": None, "score": 0.0, "cover": 0.0, "n": 0}
        out.append(rec)
        tl, td = tokens(ln)
        t = tl or td                  # a line without letters ("(2)", "٣٦") is matched on its digits
        n = sum(t.values())
        rec["n"] = n
        if not n:
            continue
        # page from the n-gram alignment, then its neighbours
        p0 = max(0, sum(1 for st in starts if st <= s) - 1)
        best = None
        for p in (p0, p0 + 1, p0 - 1):
            if not 0 <= p < len(pages):
                continue
            for k, (fl, fd) in enumerate(fps[p]):
                common = sum((t & (fl if tl else fd)).values())
                if not common:
                    continue
                sc = common / n
                cover = common / max(1, sum(fl.values()) + (0 if tl else sum(fd.values())))  # the PDF line this text fills
                dsc = round(sum((td & fd).values()) / sum(td.values()), 2) if tl and td else 1.0
                y0 = pages[p]["lines"][k]["bbox"][1]
                after = (p, y0) >= prev if prev[0] is not None else True
                # a short fragment ("(2)", "في") is best identified by the PDF fragment it fills exactly;
                # a real line by reading order (identical lines recur: running headers, repeated phrases)
                # identical lines (running headers) recur on every page: the one nearest the previous line wins
                near = -abs(p - prev[0]) if prev[0] is not None else 0
                if n < 3:
                    first = (round(sc, 3), dsc, round(cover, 2), near, after)
                else:
                    first = (round(sc, 3), dsc, after, round(cover, 2), near)
                key = first + (p == p0, -abs(p - p0), -y0 if after else y0)
                if best is None or key > best[0]:
                    best = (key, p, k, sc, cover)
        if best:
            key, p, k, sc, _ = best
            rec.update(page_index=p + 1, line=k, score=round(sc, 3), cover=round(best[4], 2))
            if sc >= 0.6 and n >= 3:   # only well-identified lines steer the reading order
                prev = (p, pages[p]["lines"][k]["bbox"][1])
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--txt", required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--pdf")
    g.add_argument("--pdf-text-json")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    txt = open(a.txt, encoding="utf-8").read()
    pages = pdf_pages(a.pdf) if a.pdf else json.load(open(a.pdf_text_json, encoding="utf-8"))
    res = align(txt, pages)
    out = res
    if any(pg.get("lines") for pg in pages):
        for r, pg in zip(res, pages):
            r["height"] = pg.get("height")
            r["lines"] = pg.get("lines", [])
        tl = match_lines(txt, pages, res)
        out = {"pages": res, "txt_lines": tl}
    with open(a.out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    m = sum(1 for p in res if p["evidence"] == "pdf_text_alignment")
    print(f"pages: {len(res)}  matched: {m}  interpolated: {len(res)-m}  -> {a.out}")
    if out is not res:
        ok = sum(1 for r in out["txt_lines"] if r["score"] >= 0.6)
        print(f"line layout: {ok}/{sum(1 for r in out['txt_lines'] if r['n'])} text lines matched to a PDF line")


if __name__ == "__main__":
    main()
