---
name: book-structure-analyzer
description: Analyze an OCR-extracted Arabic heritage book (hadith, tafsir, fiqh, history, biography, poetry, adab, etc.) and discover its real internal structure, producing book_profile.json, structure.json, units.json and issues.json with exact source provenance. Use this skill whenever a book's OCR text must be split into structural nodes and content units, whenever OCR output of a turath book needs structural analysis, or when re-running/validating a previous structure analysis.
---

# Book Structure Analyzer

## Purpose

Analyze an OCR-extracted Arabic heritage book and discover its internal structure from the source itself.

The book's structure is not known in advance. The analyzer must infer it from the document rather than assume that every book is built from the same kinds of units.

The output feeds later pipeline stages: entity extraction, relationship extraction, knowledge-graph construction, search, citation, and content generation. Those stages will trust this output, so an error here (a footnote attached to the wrong unit, a missing isnad, an inflated confidence) propagates into everything built on top of it. Precision and honesty about uncertainty matter more than looking complete.

---

## Core Principles

1. **Discover, don't impose.** The hierarchy and the unit type come from the book.
2. **The raw source is sacred.** Never rewrite, reorder, or correct the source text. Every correction, relocation, or reinterpretation is recorded *beside* the source, never *instead of* it.
3. **Reading order ≠ logical order.** OCR serializes a two-dimensional page into one line of text. Footnotes, marginal headings, running headers, and page numbers end up wherever the OCR engine happened to read them. A piece of text belongs to the unit it *logically* belongs to, not to the unit it happens to sit next to in the file.
4. **Confidence must be honest.** A unit whose text is known to be incomplete or contaminated cannot carry high confidence.
5. **Everything is locatable.** Every node, unit, segment, and issue points to exact character offsets in the source file.
6. **Characters, not lines.** The unit of classification is the character range, not the OCR line. One OCR line often mixes things that belong to different places (the end of a footnote glued to the next heading, the last words of a matn followed by a page header). Split the line at the exact boundary; never classify a whole line by its most visible part.
7. **Exactly once.** Every non-whitespace character of the source is claimed by exactly one owner: a unit segment, a noise span, or a front/back-matter node segment. No character is left unclaimed and none is claimed twice. `scripts/validate.py` enforces this.

Example structures (illustrative only, NOT a taxonomy):

- Hadith collection → books, chapters, hadiths
- History → periods, years, events
- Biographical dictionary → tabaqat, biographies
- Tafsir → surahs, verse groups, commentary
- Fiqh → kitab, bab, masa'il, opinions
- Poetry → diwan sections, poems, verses
- Adab / literature → chapters, stories, anecdotes
- Rihla → journeys, locations, episodes

---

# Input

## Primary input

An OCR-extracted text file (e.g. `book-ocr.txt`). This is the authoritative text for this stage.

## Optional auxiliary inputs

If present in the project, use them — they solve problems the plain text cannot:

- **Searchable PDF** (PDF with OCR text layer): its text is already split per page. Use it to establish page boundaries and to decide which footnotes belong to which page. Use it as a *reference*, not as a replacement for the primary text.
- **DOCX export**: may preserve heading styles and page breaks.
- **Page images**: the final authority when OCR is ambiguous.

If an auxiliary input is used, record which one and how in `book_profile.json` → `sources_used`.

If no auxiliary input exists, say so, and record the resulting limitations as issues rather than guessing.

---

# Workflow: Two Phases

## Phase A — Proposal (default when run on a new book)

Analyze the book and **report** — do not write output files yet:

- metadata found in the source
- apparent genre
- discovered hierarchy levels and how each was detected
- the unit type and the internal segment roles found inside units (see Stage 6)
- how pages, footnotes, and headers are laid out in this OCR
- the main OCR/structural problems
- 2–3 sample units, including at least one *difficult* one (e.g. a unit spanning a page break or with displaced footnotes), shown with their segments

Then stop and wait for approval.

## Phase B — Generation

After approval (or if explicitly told to generate directly):

1. Build the outputs **with a script** that computes every offset from the source text (Stage 7).
2. Run `scripts/validate.py` (Stage 11).
3. Fix and re-run until the status is `pass`.
4. Report the final validation summary to the user.

The run is not finished until `validation_report.json` exists and says `pass`.

---

# Processing Pipeline

## Stage 1 — Document Profiling

Inspect the whole document (or as much as practical, then sample systematically from beginning, middle and end).

Determine, from the source only:

- title, author, editor / muhaqqiq / commentator / translator if stated
- publisher, edition, year, ISBN if stated
- apparent genre
- language(s)
- numbering systems (Western digits, Arabic-Indic digits ٠-٩, Persian digits ۰-۹ — OCR often mixes them; treat them as equivalent for numbering but keep the original characters in the text)
- recurring patterns: running headers, page-number formats, footnote markers, heading formats
- front matter, main content, back matter (indexes, TOC, bibliography, colophon)

**Do not invent metadata.** Use `null` when not supported by the source. Do not identify an author from outside knowledge.

### Expected-component check

For the apparent genre, list components such books *commonly* contain (e.g. author's introduction / khutbat al-kitab, editor's introduction, TOC, index) and check whether each is **present, absent, or undetermined** in the source. Record absences as issues of type `possibly_missing_component` — never create a node for a component that is not in the text. The absence may be real (the edition omits it) or an OCR loss; the reviewer decides.

---

## Stage 2 — Page Model

Before splitting anything, establish page boundaries, because footnote attribution and noise detection depend on them.

Evidence for page boundaries, strongest first:

1. Per-page text from a searchable PDF / DOCX page breaks
2. Form-feed characters or explicit page markers in the TXT
3. Running headers (repeated book/chapter title) and page numbers
4. Footnote blocks (`(1) أخرجه ...`) — they usually close a page

Produce a page list:

```json
{ "page_index": 17, "printed_page_number": "١٨", "char_start": 15020, "char_end": 15980, "evidence": "running_header+page_number", "confidence": 0.85 }
```

`printed_page_number` is the number printed in the book (may differ from `page_index`). If boundaries cannot be established, use `null` and record an issue — **never invent page numbers.**

Store the page list in `structure.json` → `pages`.

---

## Stage 3 — Artifact & Noise Detection

Classify spans that are not part of the author's/editor's text flow:

| Kind | Examples |
|---|---|
| `running_header` | book title repeated at the top of pages |
| `page_number` | isolated `١٨`, `•۱۸`, `- 18 -` |
| `decorative` | ornaments, garbled glyphs from calligraphy (e.g. non-Arabic characters produced from a decorated basmala) |
| `duplicate_fragment` | OCR reading the same words twice (`عن أبي عن أبي`) |
| `displaced_heading` | a heading or marginal title read at the wrong position |
| `displaced_fragment` | text read out of order (e.g. the start of an isnad appearing before the heading) |
| `separator` | stray `-`, `«` alone on a line |

Record each as a **noise span** with offsets. Do not delete it from the source. Noise spans are later referenced by segments (Stage 6) so that a clean view can be built without losing the raw text.

### Noise must be minimal

A noise span covers **only the noise characters** — never a word of the author's or editor's text.

- Garbled `M` at the end of a gharib line → the noise span is the one character `M`; the rest of the line is a `gharib` segment.
- `(٢) أخرجه مسلم في الإيمان (٩٥)الكسب الحلال` → takhrij segment `(٢) أخرجه مسلم في الإيمان (٩٥)` + displaced-heading span `الكسب الحلال`. Not one noise span.
- `…ما لا يعنيه » . [ رواه الترمذي ] (۲)` followed by a running header → matn / attribution / anchor segments, then a `running_header` noise span for the header only.

Why: later stages build the reading text by *removing* noise. Any real text inside a noise span is silently deleted from the book.

A displaced heading is noise at the place it was misread; the unit's real heading remains its `heading` segment. A duplicate of real text is noise only for the duplicate copy.

A repeated pattern may be recorded once with all occurrence offsets.

---

## Stage 4 — Structure Discovery

Discover the hierarchy used by the author/editor from evidence such as:

- explicit headings and structural words (كتاب، باب، فصل، جزء، مسألة، ذكر، ترجمة…)
- numbered entries
- repeated title patterns
- opening/closing formulas (`عن…قال`، `حدثنا`، `قال المصنف`، `رواه…`)
- the book's own TOC/index

Do not assume a word such as "باب" or "كتاب" always means the same level; infer levels from context.

### Heading verification

Cross-check every heading found in the body against the book's TOC/index (if present), in **both directions**:

- body heading with no TOC entry → issue
- TOC entry with no body heading → issue (possible lost unit)
- body and TOC disagree → keep the body text as `title`, record the TOC version in `title_variants`, raise an issue

### Truncated headings and honorific ligatures

OCR frequently fails on ligatures and decorative honorifics: ﷺ، ﷻ، عز وجل، جل وعلا، رضي الله عنه، رحمه الله، ﷿. A heading or sentence that ends abruptly (`فضل الله عل`، `رسول الله` with nothing after it where ﷺ is expected) is likely a ligature failure, not a genuinely shorter text.

- Keep the OCR text as-is.
- Record an issue of type `probable_ligature_loss` with the plausible reading(s) as *candidates*, marked `needs_visual_check: true`.
- Do not pick one candidate as fact.

---

## Stage 5 — Structure Model

Represent the hierarchy as a tree. Each node:

```json
{
  "id": "node_012",
  "type": "chapter",
  "title": "…",
  "title_variants": [],
  "ordinal": 5,
  "number_raw": "٥",
  "parent_id": "node_002",
  "children": ["node_013"],
  "source": { "char_start": 1250, "char_end": 5420, "page_start": 3, "page_end": 5 },
  "evidence": "explicit heading + numbered entry",
  "confidence": 0.95
}
```

`type` is descriptive, not a fixed vocabulary.

### Ordinal vs. printed number

- **`ordinal`** — the logical position of the node/unit among its siblings of the same type (1, 2, 3…). Always an integer, never `null`, whenever the order is established by the text, the headings, or the TOC. Use the book's own numbering scheme when it exists (if the book numbers hadiths 1–42, ordinals are 1–42).
- **`number_raw`** — the number as printed in the OCR, exactly as written (`"٢٤"`, `"۲۴"`, `"24"`), or `null` if no printed number was found near this unit.

A missing printed number is a textual observation (`number_raw: null`, low-severity issue if useful). It does not make the order unknown. Only when the order itself is genuinely uncertain does the ordinal get low confidence and an issue — it is still filled in.

Nodes that hold text directly rather than units (front matter, TOC/index, colophon) carry `segments` (e.g. roles `title_page`, `publication_data`, `toc_entry`) so that their characters are claimed too (Principle 7).

Units are **not** duplicated as nodes. A leaf node may *contain* units, or units may hang directly from a chapter-level node — whichever reflects the book. If each unit already has its own heading (e.g. each hadith has a topic title), the unit carries that title; do not also create a node per unit unless the node adds real structure.

---

## Stage 6 — Units and Internal Segments

### Units

A unit is a coherent, citable piece of source material: one hadith, one biography, one poem, one event, one legal issue, one Q&A.

- Do not split on paragraph breaks alone.
- Do not merge independent units because they share a chapter.

### Unit extent vs. unit content

Distinguish two things:

- **`span`** — the contiguous character range in the OCR where the unit mainly lives (from its heading/opening to the start of the next unit).
- **`segments`** — the pieces of text that *logically belong* to the unit. Segments may lie **outside** the span (a footnote printed on the next page, an isnad fragment displaced before the heading), and some text inside the span may belong to **another** unit or be noise.

This distinction is the heart of correct provenance. Never assume that everything inside the span belongs to the unit.

### Segment roles

Inside each unit, classify text into roles. Roles are discovered per book; common ones:

| Genre | Typical roles |
|---|---|
| Hadith | `heading`, `number`, `isnad`, `matn`, `source_attribution` (رواه…), `takhrij` (editor footnote), `gharib` (word explanation), `sharh` |
| Tafsir | `ayah`, `tafsir`, `qira'at`, `footnote` |
| Poetry | `verse`, `sharh`, `occasion` |
| History / biography | `heading`, `narrative`, `quotation`, `footnote` |
| Fiqh | `mas'ala`, `opinion`, `evidence`, `tarjih`, `footnote` |

Also always available: `editorial_note`, `footnote`, `noise_ref` (pointer to a noise span).

Each segment:

```json
{
  "role": "takhrij",
  "char_start": 14210,
  "char_end": 14268,
  "page_index": 17,
  "footnote_marker": "(1)",
  "placement": "displaced",
  "confidence": 0.85
}
```

`placement` is `in_span` (inside the owner unit's span) or `displaced` (outside it). Displaced segments must have an explanation in the related issue.

### Segment rules

- **Character-level boundaries.** A segment starts and ends at the exact characters where the role changes, even in the middle of an OCR line.
- **One segment per contiguous role run.** Consecutive lines with the same role form one segment, not one segment per line. (Exception: a run interrupted by noise or foreign text is split around it.)
- **No duplicates.** The same role + offsets appears once.
- **Mirroring.** Every `displaced` segment lies inside some other unit's span (or in front/back matter). That host unit must list a `foreign_spans` entry with **exactly the same offsets** and `belongs_to` = the owner unit. Conversely, every foreign span that names a unit must correspond to a segment of that unit with the same offsets. Foreign spans of one unit never overlap each other.
- **Specific roles first.** Use the genre's specific roles (for hadith: `isnad`, `matn`, `source_attribution`, `anchor`, `takhrij`, `gharib`). Where the boundary is uncertain, still use the specific role with lower confidence (e.g. 0.6) and let the issue explain. Use the generic `body` only when you cannot even guess — if most segments of a book end up as `body`, the analysis has not been done.
- Footnote anchor markers inside the text (`(1)`, `(٢)`) are their own `anchor` segments, so that the marker can be matched to its footnote.

### Footnote attribution rules (all genres)

Footnotes are the most common source of misattribution. Apply these rules:

1. A footnote belongs to the **anchor** carrying the same marker `(1)`, `(٢)`, `*` on the **same page**. Match by marker within the page, never by linear proximity in the text.
2. Footnotes printed at the bottom of a page are frequently read by OCR *after* the next page's heading, so they appear inside the **following** unit. Expect this pattern and check for it explicitly.
3. Content check: if a footnote explains a word or cites a source that does not appear in the unit's text but does appear in the previous unit, it belongs to the previous unit.
4. If the anchor cannot be determined, attach the footnote to the most probable unit with confidence ≤ 0.5 and raise an issue — do not silently attach it with high confidence.
5. **Marker inventory for every unit — including the first and the last.** For each unit, list its anchors and its footnotes. A footnote whose marker has no anchor in the host unit, while an unmatched anchor with that marker exists in the previous unit, belongs to the previous unit. Apply this to every unit; edge units (first, last, before/after front or back matter) are where the pattern is most often missed.

### Completeness check per unit

For each unit, check whether its expected parts are present (for a hadith: opening of isnad, matn, attribution). If a part appears to be missing or displaced, look for it nearby (before the heading, in the previous unit, after the page's footnotes) and either link it as a displaced segment or raise an issue of type `incomplete_unit`.

---

## Stage 7 — Source Preservation and Offsets

Every node, unit, segment, noise span, and issue carries exact offsets.

**Offset definition (mandatory):**

- Offsets are **Unicode code-point indices** into the source file decoded as UTF-8, with its line endings exactly as they are on disk (do not convert CRLF ↔ LF before indexing).
- `char_end` is exclusive: the text of a span is `source[char_start:char_end]`.
- Record in `book_profile.json`: `source_file`, `source_sha256`, `source_length_chars`, `line_endings`.

**Offsets must be computed by code, not estimated.** Locate spans by searching the actual text in a script and check that `source[char_start:char_end]` equals the stored text exactly. Never write offsets by hand or by estimation.

Never use line numbers as the primary locator. Line numbers may be added as a convenience but offsets are authoritative.

**Canonical field names.** Offsets are always named `char_start` / `char_end` — in nodes, units, spans, segments, noise spans, foreign spans, pages, and issue occurrences. Never abbreviate (`cs`, `ce`, `start`, `end`). When an issue points at an object that also exists as a segment/foreign span/noise span, it uses **the same offsets** as that object.

---

## Stage 8 — Confidence

Confidence (0.0–1.0) measures confidence in the **structural interpretation**, not historical truth.

| Value | Meaning |
|---|---|
| 0.95 | explicit heading/numbering, verified against TOC, clean boundaries |
| 0.80 | strong contextual evidence |
| 0.60 | plausible but uncertain |
| 0.35 | weak inference |

### Propagation rules

- A unit's confidence **≤** the confidence of the node it belongs to.
- Every open issue of severity `medium` or `high` attached to a unit lowers its confidence (suggested: −0.05 per medium, −0.15 per high), and the unit lists those issue IDs in `issue_ids`.
- A unit with a displaced/unattributed part, a missing part, or foreign content inside its span may not exceed 0.80.
- Units must not all share the same confidence value by default; if they do, re-check.

### Issue confidence

For issues, `confidence` means **"probability that this issue is real"**. Do not raise issues you believe are probably not real (confidence < 0.4) unless they have high impact; a valid classical word is not an OCR error just because it is uncommon. Before flagging a word as an OCR error, state why (not a known word, breaks the grammar, contradicts the same word elsewhere, etc.).

---

## Stage 9 — Issues

Every issue has a location and is actionable.

```json
{
  "id": "issue_014",
  "type": "misattributed_footnote",
  "severity": "high",
  "location": { "unit_id": "unit_024", "char_start": 14210, "char_end": 14268, "page_index": 17 },
  "related_unit_ids": ["unit_023"],
  "text": "(1) أخرجه …",
  "description": "Footnote found inside unit_024's span; its content refers to unit_023.",
  "proposed_action": { "kind": "reassign_segment", "to_unit_id": "unit_023" },
  "candidates": [],
  "needs_visual_check": false,
  "confidence": 0.85
}
```

Suggested types (extend as needed): `repeated_header`, `page_number_noise`, `garbled_glyph`, `duplicate_fragment`, `displaced_fragment`, `displaced_heading`, `misattributed_footnote`, `unanchored_footnote`, `incomplete_unit`, `probable_ligature_loss`, `truncated_heading`, `heading_toc_mismatch`, `probable_ocr_error`, `ambiguous_level`, `page_boundary_unknown`, `possibly_missing_component`.

Severity guide:

- `high` — affects which unit text belongs to, or loses text
- `medium` — affects a title, a boundary, or a structural level
- `low` — cosmetic or single-character OCR problem

Group issues that are the same pattern repeated many times into one issue with a list of `occurrences` (each with offsets), instead of dozens of separate issues.

### OCR corrections

When a correction is highly likely, keep the original and record the proposal:

```json
{ "original": "أعللت", "proposed": "أحللت", "reason": "context and parallel wording in the unit", "confidence": 0.8 }
```

---

## Stage 10 — Scope Separation

This skill performs **structure discovery only**. Do NOT:

- extract entities or relationships
- summarize or interpret content
- fact-check or grade hadiths
- enrich from external sources or general knowledge (do not expand `قال فلان` to a full name that is not in the source)

Segment roles (isnad, matn, takhrij…) are structural, not semantic extraction, and are in scope.

---

## Stage 11 — Validation (mandatory before finishing Phase B)

Validation is done by the bundled script, not by self-assessment:

```bash
python scripts/validate.py --source <book-ocr.txt> --out <output_dir>
```

(`scripts/` is inside this skill's folder. Standard-library Python 3, no installs needed.)

It writes `<output_dir>/validation_report.json` and exits `0` on pass, `1` on errors. It checks:

1. encoding (UTF-8, no BOM, LF), JSON validity, canonical field names
2. `source_sha256`, `source_length_chars`, `line_endings` against the real source
3. `raw_text == source[span]` and any stored `text` against its offsets
4. **coverage**: every non-whitespace character claimed exactly once (gaps and double claims are listed with the text)
5. duplicate segments, overlapping unit spans, overlapping foreign spans
6. displaced segment ↔ foreign span mirroring (exact offsets both ways)
7. `placement` consistent with the span
8. references: parents, children, issue ids, `belongs_to`, noise ids, occurrence unit ids
9. `ordinal` present, unique and consecutive within each sibling group
10. confidence rules (Stage 8) and issue confidence present and in [0, 1]
11. issue occurrences touch their unit and reuse the exact offsets of the object they point to
12. warnings for over-long noise spans and a high share of the generic `body` role

### Rules

- Loop: fix the outputs → re-run → until `status: pass`. Fix the **data**, i.e. the script that produced it; do not hand-patch JSON.
- **Never edit `validate.py`** to make a check pass, and never delete content to make coverage pass. If a check seems wrong for this book, stop and report it to the user with the example.
- Review the warnings too. Each remaining warning should be either fixed or explained in the final report.
- Also do, by reasoning (the script cannot): the TOC cross-check in both directions (Stage 4) and the marker inventory (Stage 6).
- Deliver `validation_report.json` together with the four output files, and show the user its summary (status, error/warning counts, stats).

---

# Output Files

Five files are delivered: the four below plus `validation_report.json` (Stage 11).

Write all files with a script (e.g. Python `json.dump(..., ensure_ascii=False, indent=2)`), **UTF-8 without BOM, LF line endings**. Do not use tools that add a BOM or change encoding (e.g. PowerShell `Out-File`/`Set-Content` default encodings). Re-open each file after writing and parse it to confirm it is valid.

## 1. `book_profile.json`

```json
{
  "title": "…",
  "author": "…",
  "editor_commentator": null,
  "publisher": null,
  "edition": null,
  "publication_year": null,
  "isbn": null,
  "genre": "…",
  "language": "ar",
  "source_file": "book-ocr.txt",
  "source_sha256": "…",
  "source_length_chars": 0,
  "line_endings": "CRLF",
  "sources_used": ["book-ocr.txt", "book-searchable.pdf (page boundaries only)"],
  "expected_components": [
    { "component": "author_introduction", "status": "absent_or_undetermined", "issue_id": "issue_003" }
  ],
  "segment_roles_used": ["heading", "isnad", "matn", "takhrij"],
  "structure_summary": "…",
  "notes": []
}
```

Only values supported by the source.

## 2. `structure.json`

```json
{
  "root": "node_000",
  "pages": [ /* Stage 2 page list, or [] if unknown */ ],
  "noise_spans": [ /* Stage 3 */ ],
  "nodes": [ /* Stage 5 */ ]
}
```

## 3. `units.json`

```json
{
  "units": [
    {
      "id": "unit_001",
      "type": "hadith",
      "ordinal": 1,
      "number_raw": "١",
      "title": "…",
      "title_variants": [],
      "parent_id": "node_002",
      "span": { "char_start": 1168, "char_end": 1929, "page_start": 1, "page_end": 2 },
      "raw_text": "source[span.char_start:span.char_end], unchanged",
      "segments": [
        { "role": "isnad", "char_start": 1050, "char_end": 1080, "placement": "displaced", "confidence": 0.7 },
        { "role": "heading", "char_start": 1168, "char_end": 1183, "placement": "in_span", "confidence": 0.95 },
        { "role": "isnad", "char_start": 1184, "char_end": 1262, "placement": "in_span", "confidence": 0.75 },
        { "role": "matn", "char_start": 1263, "char_end": 1541, "placement": "in_span", "confidence": 0.8 },
        { "role": "source_attribution", "char_start": 1542, "char_end": 1790, "placement": "in_span", "confidence": 0.85 },
        { "role": "anchor", "char_start": 1791, "char_end": 1794, "placement": "in_span", "confidence": 0.9 },
        { "role": "takhrij", "char_start": 1803, "char_end": 1850, "footnote_marker": "(1)", "placement": "in_span", "confidence": 0.85 },
        { "role": "noise_ref", "noise_id": "noise_004" }
      ],
      "foreign_spans": [
        { "char_start": 1850, "char_end": 1900, "belongs_to": "unit_002", "issue_id": "issue_012" }
      ],
      "issue_ids": ["issue_002"],
      "confidence": 0.8
    }
  ]
}
```

- `raw_text` is exactly the span, never cleaned.
- A clean/reading view is **derived** later from the segments; do not store cleaned text as if it were the source.
- `foreign_spans` lists text inside the span that belongs to **another unit** (use the exact offsets of that unit's displaced segment). Noise inside the span is not listed here; it is a noise span referenced by `noise_ref`.
- `segments` are listed in source order.

## 4. `issues.json`

```json
{ "issues": [ /* Stage 9 */ ] }
```

Every issue has a numeric `confidence`. Grouped issues use `occurrences: [{ "unit_id", "char_start", "char_end", "note" }]`; single issues may use `location` with the same fields.

---

# Rules for Arabic Text

1. Preserve Arabic text exactly: spelling, diacritics, classical vocabulary, digits as written.
2. Do not normalize unless explicitly asked; normalized forms go in separate fields.
3. Do not silently correct OCR.
4. Treat honorific ligatures and decorative formulas with care (Stage 4).
5. Distinguish **structural certainty** from **textual uncertainty**: a unit can be structurally certain while containing uncertain OCR, and vice versa.

---

# No Hallucination Rule

Never add information from general knowledge. The source-first principle is mandatory. When general knowledge suggests something (e.g. what a truncated title probably was, or that a book normally has an introduction), it may appear only as a *candidate* inside an issue, flagged for human review — never in titles, text, or metadata.

---

# Scalability

The same skill must work from 20 pages to 1,000+ pages.

For large books:

- Build the page model and the global heading list first, across the whole book.
- Process in chunks aligned to **page or structural boundaries**, never arbitrary character counts.
- Handle footnotes and displaced fragments that cross chunk boundaries by looking one page ahead/behind.
- Keep IDs global and stable; the final structure must not depend on chunk boundaries.

---

# Future Compatibility

Consumers: entity extractor, relationship/event extractor, knowledge-graph builder, semantic search indexer, citation/provenance layer, content generator.

Therefore:

- IDs are stable across re-runs of the same source (derive them from order in the book, not from processing order).
- Offsets are exact and verified.
- Downstream stages should consume **segments** (e.g. only `matn` for indexing hadith text, `takhrij` for sources), not raw spans.

---

# Final Principle

The goal is not to make the book look like a database.

The goal is to make the database **reflect the book's own structure** — and to say clearly where the OCR makes that structure uncertain.