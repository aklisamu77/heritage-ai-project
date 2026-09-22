#!/usr/bin/env python3
"""
extract_pages.py — build a page model (pages.json) for an OCR text file from the
searchable PDF produced by the same OCR run.

Searchable-PDF text layers are often scrambled (right-to-left extraction reorders words),
so pages are aligned to the TXT by fuzzy n-gram matching on diacritic-free, whitespace-free
text, with page order enforced. Pages that cannot be matched are interpolated and get lower
confidence. engine.py later snaps these boundaries to running headers / page numbers found
in the TXT, and marks the ones it could not confirm.

Usage:
  python extract_pages.py --txt book.txt --pdf book-searchable.pdf --out pages.json
  python extract_pages.py --txt book.txt --pdf-text-json pages_text.json --out pages.json
      (pages_text.json = [{"text": "..."} , ...] one entry per PDF page, in order)

Needs PyMuPDF (pip install pymupdf) or pypdf (pip install pypdf) for --pdf.
"""
import argparse
import json
import re
import sys

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
        import fitz  # PyMuPDF
        doc = fitz.open(path)
        return [{"text": p.get_text()} for p in doc]
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
    with open(a.out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    m = sum(1 for p in res if p["evidence"] == "pdf_text_alignment")
    print(f"pages: {len(res)}  matched: {m}  interpolated: {len(res)-m}  -> {a.out}")


if __name__ == "__main__":
    main()
