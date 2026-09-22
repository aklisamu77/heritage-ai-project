---
name: book-structure-analyzer
description: Discover the real structure of an OCR-extracted Arabic heritage book (hadith, tafsir, fiqh, history, biography, poetry, adab…) and produce book_profile.json, structure.json, units.json, issues.json with exact character provenance, using the bundled engine, a per-book config, and a validator. Use whenever a turath book's OCR text must be split into structural nodes and content units, when re-running or checking such an analysis, or when the user says to analyze / structure / segment a book for the heritage project.
---

# Book Structure Analyzer

## Purpose

Turn the OCR text of an Arabic heritage book into a reliable structural map: which text is front matter, which is each unit (hadith, biography, poem, mas'ala…), which part of a unit is its heading, main text, attribution, editor's footnotes, and which characters are OCR noise — every piece pointing to exact offsets in the source.

Later stages (entity extraction, knowledge graph, search, citation, content generation) trust this map. An error here propagates everywhere, so honesty about uncertainty matters more than looking complete.

---

## How this skill is built — read first

The work is split between **code that never changes per book** and **two small files you write per book**:

```
book-structure-analyzer/
├── SKILL.md
├── scripts/
│   ├── engine.py          generic rules: regions, headings, numbers, footnotes, anchors,
│   │                      attribution, noise, pages, confidence, issues  (same for every book)
│   ├── extract_pages.py   page model from the searchable PDF
│   └── validate.py        mandatory consistency checks
├── templates/
│   ├── book_config.template.json
│   └── overrides.template.json
└── examples/
    ├── al-arbaeen-nawawiya/   hadith collection: 42 headed units, editor footnotes (takhrij/gharib)
    └── asul-elsona/           creed treatise in a critical edition: introduction + multi-level study
                               (sections), treatise preamble, 8 numbered paragraphs, facsimile noise range
```

**Your job (the AI):** read the book, understand it, and express that understanding as
- `book_config.json` — what this book looks like (its headings in order, running header, patterns, back matter…);
- `overrides.json` — the few individual judgment calls the rules cannot make, each with a reason.

**The engine's job:** apply the same rules to every book and label every character exactly once.

### Hard rules

1. **Never write a new builder or per-book script.** No code that hard-codes line numbers, unit ranges, footnote assignments or confidence values for a specific book. That is the failure this design exists to prevent: it produces output that looks validated but is really a hand annotation that cannot be reused or trusted.
2. **Never edit the output JSON by hand.** Change the config or the overrides and rebuild.
3. **Never edit `validate.py`** to make a check pass, and never delete or relabel real text to make coverage pass.
4. **If the engine cannot handle something in a new book**, stop and tell the user. The fix is a *generic* rule added to `engine.py`, controlled by a config key, that would apply to any book with the same pattern — and it must be approved by the user and re-tested on `examples/`. Book-specific exceptions go in `overrides.json`, never in the engine.

---

## Core Principles

1. **Discover, don't impose.** Hierarchy, unit type and roles come from the book.
2. **The raw source is sacred.** Nothing is rewritten, reordered or corrected. Corrections are recorded beside the source.
3. **Reading order ≠ logical order.** OCR flattens the page: page-bottom footnotes land after the next heading, marginal headings land mid-text, anchors land after the next heading. Text belongs to the unit it logically belongs to.
4. **Characters, not lines.** One OCR line often mixes roles; boundaries fall at the exact characters.
5. **Exactly once.** Every non-whitespace character has exactly one owner: a unit segment, a noise span, or a front/back-matter node segment.
6. **Honest confidence.** Displaced or uncertain content lowers confidence; the engine computes it from rules, never by hand.
7. **Structure, not interpretation.** Splitting a hadith into isnad and matn, identifying narrators, grading — all belong to later stages. Here a hadith's main text is one role (`hadith_text`).

---

## Workflow

### Phase A — Understand the book and write the config (then stop)

1. Copy `templates/book_config.template.json` to the project (e.g. `work/<book>/book_config.json`). Look at `examples/` for a finished one.
2. Run discovery (changes nothing):
   ```bash
   python scripts/engine.py discover --source <book.txt>
   ```
   It lists repeated lines (running-header candidates), heading candidates (short undiacritized lines followed by diacritized text), footnote and marker counts.
3. **Read the book** — front matter, several units from the beginning, middle and end, the TOC/index. Decide:
   - metadata stated in the source;
   - the unit type and the body role name;
   - the running header(s);
   - the ordered list of unit headings **as spelled in the body** (use the TOC to check order and completeness; record TOC spellings as `variants`);
   - subtitles, back-matter start lines, expected components (e.g. author introduction present or not);
   - **sections outside the units** — the editor's introduction, the study, the author's biography and its sub-parts, manuscript descriptions — with their levels (use the book's TOC). Each becomes a node, so its text is not lost in one "front matter" block;
   - **where the main text starts** (`main_start_at`) if something of the author precedes unit 1 (a chain of transmission of the whole book, the author's khutba);
   - **OCR digit look-alikes** seen in markers/numbers (`ه` for ٥, `V` for ٧, `Ʌ` for ٨, `ε` for ٤) — only those you actually see used as digits.
4. Fill `book_config.json`. Every value must be supported by the source.
5. Run `python scripts/engine.py discover --source <book.txt> --config book_config.json` — `config_headings_not_found` must be empty.
6. **Report to the user and stop:** metadata, genre, unit type, number of units, how headings were found, the running header, known problems (scattered isnad before headings, glued headings, missing TOC entries…), and the overrides you expect to need. Wait for approval.

### Phase B — Build, validate, review, refine

1. **Page model** (if a searchable PDF exists):
   ```bash
   python scripts/extract_pages.py --txt <book.txt> --pdf <book-searchable.pdf> --out pages.json
   ```
2. **Build:**
   ```bash
   python scripts/engine.py build --source <book.txt> --config book_config.json \
          --overrides overrides.json --pages pages.json --out <output_dir>
   ```
3. **Validate:**
   ```bash
   python scripts/validate.py --source <book.txt> --out <output_dir>
   ```
   Must end with `status: pass`. Review every warning.
4. **Review what the validator cannot see** (checklist below). For each problem decide:
   - a pattern that affects several units → fix `book_config.json`;
   - a single local case → add an entry to `overrides.json` with a reason;
   - a pattern the engine has no rule for → stop and tell the user (Hard rule 4).
5. Rebuild and re-validate until clean. Report to the user: validation summary, unit count, issue summary by type, overrides used (with reasons), and anything left for human visual check.

### Review checklist (after every build)

- **Headings:** the 5 lowest-confidence units — is the chosen heading the real one? Any `glued_heading` / `heading_not_found` issues?
- **Footnotes:** read every occurrence of `displaced_footnote` and `unanchored_footnote`. Does the footnote's content (the words it explains, the source it cites) match the unit it was assigned to?
- **Trailing fragments:** each `trailing_fragment` is either legitimate (second narration, author's comment, continued attribution) or a displaced fragment → override if displaced.
- **Noise:** skim `noise_spans`; no real word of the author or editor may be noise.
- **First and last unit, and units around front/back matter** — the most error-prone places.
- **Front matter:** does the first unit start where its text really starts (isnad fragments are often read before the heading)?

---

## Config keys beyond the basics

| key | use |
|---|---|
| `sections` | ordered list `{title, level, type, start_at:{text, occurrence?}, heading: true/false}`. Located in order before the main text; a section runs to the next section of the same or higher level. `heading: false` = the start text is not a heading line (e.g. a parent like «الدراسة» that starts where its first child starts, or an introduction without a heading). `occurrence` counts over the whole front region; without it the first match after the previous section is used. |
| `main_start_at` | `{text, occurrence, heading}` — the main text starts here (before unit 1). Text between it and unit 1 becomes the main node's `preamble`, with its own footnotes/anchors. |
| `digit_lookalikes` | `{"ه": "5", …}` — letters OCR produced for digits; used in number prefixes and footnote markers only. |
| `footnote_continued_mark` | default `=`; footnotes ending with it get a `continued_footnote` issue (their continuation on the next page is usually read as main text — check it). |

Footnotes inside sections and the preamble are labeled as the node's `footnote`/`anchor` segments (no ownership search). Page numbers glued to a heading or to the first word of a main-text line (`٣٦لم يكن`) become `page_number` noise; if the glued number equals the unit's ordinal it becomes the unit's `number`.

## Known limit: footnote continuations

Plain OCR text loses the page layout. When a footnote continues on the next page, its continuation is usually read as main text and the rules cannot reliably tell them apart (especially in undiacritized books). The engine flags each such case (`continued_footnote`, `unanchored_footnote`, `trailing_fragment`); fix confirmed cases with `assign` / `set_role` overrides. A layout-aware page model (block positions from the PDF) is the planned fix.

## What the engine does (so you know what to configure)

In order, over a per-character label array:

1. **Regions** — front matter = text before the first unit; back matter from each `back_matter.start_at`.
2. **Headings** — every occurrence of each configured heading; the real one is the latest standalone occurrence before the next unit's heading (earlier copies are marginal/displaced). Units run from heading to next heading (or an override start).
3. **Numbers** — `N -` at a line start, or a standalone number equal to this or the next unit's ordinal; other standalone numbers are page numbers.
4. **Footnotes** — a line starting with a marker `(N)` + text; continuation lines while they look like footnote text (low diacritics, no body markers, no page break).
   **Ownership** among the host unit and up to `footnote_lookback` units before it:
   - an anchor with the same marker **before** the footnote in the text (+2);
   - the words the footnote explains (`قوله : « … »`) found in the unit's main text (+4 × share);
   - locality (+0.5).
   Then **block order**: an unanchored footnote followed in the same block by a higher-numbered footnote of unit U goes to the nearest unit before U that lacks that marker.
5. **Anchors** — inline `(N)`; an anchor that precedes all text of its unit (right after the heading) belongs to the previous unit.
6. **Footnote roles** — split at `footnote_split.takhrij_until` words into `takhrij` / `gharib` (or whatever roles the config names).
7. **Attribution** — `[ رواه … ]` / `( رواه … ]` or a line starting with a keyword.
8. **Noise** — running headers, non-chosen heading copies (only when undiacritized and at a line edge), separator lines, garbled glyphs. Noise covers only noise characters.
9. **Overrides** — applied last; each becomes a `manual_decision` issue.
10. **Pages** — PDF-aligned boundaries snapped to a nearby running header / page number; unconfirmed ones flagged.
11. **Assembly** — runs of identical labels become segments; displaced segments are mirrored as `foreign_spans` of their host unit; issues are grouped by type; confidence computed.

### Confidence (computed)

- heading found standalone 0.95, only glued 0.8; node 0.95
- any displaced / foreign content caps the unit at 0.8
- −0.05 per occurrence of a medium issue, −0.15 per high, −0.03 per trailing fragment in the unit
- units touched by an override ≤ 0.85
- footnote segments carry their ownership confidence: 0.9 anchor + content terms, 0.8 anchor only, 0.65 terms only, 0.55 block order, 0.5 locality only

### Overrides (ops)

| op | effect |
|---|---|
| `set_unit_start` | unit `ordinal` starts at `at` (e.g. scattered isnad read before the heading) |
| `mark_noise` | the text at `at` is noise of `type` |
| `assign` | the text at `at` belongs to unit `to_ordinal` with `role` |
| `set_role` | change the role of the text at `at` within its unit |
| `mark_noise_range` | everything from `from` to the end of `to` is noise of `type` (e.g. OCR of facsimile plates) |

`at = {"text": "...", "occurrence": 1 | "last"}` — matched ignoring diacritics and whitespace differences. Always include `reason`. If an override fails to match, the build reports an `override_failed` issue.

---

## Output files

Written by the engine (UTF-8 without BOM, LF). Plus `validation_report.json` from the validator.

### `book_profile.json`
Metadata from the config; `source_file`, `source_sha256`, `source_length_chars`, `line_endings`; `sources_used`; `config_file`, `overrides_file`, `overrides_applied`; `expected_components`; `segment_roles_used`.

### `structure.json`
```json
{
  "root": "node_000",
  "pages": [{ "page_index": 3, "printed_page_number": null, "char_start": 1604, "char_end": 2022,
              "evidence": "snapped_to_header", "confidence": 0.85 }],
  "noise_spans": [{ "id": "noise_0004", "type": "running_header", "char_start": 1910, "char_end": 1926 }],
  "nodes": [ { "id": "node_000", "type": "book", "children": ["node_001", "node_002", "node_003"], "...": "..." },
             { "id": "node_001", "type": "front_matter", "segments": [ { "role": "front_matter", "char_start": 0, "char_end": 1043 } ] } ]
}
```

### `units.json`
```json
{
  "id": "unit_024", "type": "hadith", "ordinal": 24, "number_raw": "٢٤",
  "title": "فضل الله على", "title_variants": ["فضل الله وعل (TOC)"], "parent_id": "node_002",
  "span": { "char_start": 13932, "char_end": 15660, "page_start": 17, "page_end": 18 },
  "raw_text": "exactly source[span]",
  "segments": [
    { "role": "heading", "char_start": 13932, "char_end": 13944, "placement": "in_span", "confidence": 0.95 },
    { "role": "hadith_text", "...": "..." },
    { "role": "source_attribution", "...": "..." },
    { "role": "anchor", "marker": "(1)", "footnote_id": "fn_027", "...": "..." },
    { "role": "takhrij", "footnote_id": "fn_027", "footnote_marker": "(1)", "...": "..." },
    { "role": "noise_ref", "noise_id": "noise_0041" }
  ],
  "foreign_spans": [ { "char_start": 14232, "char_end": 14268, "belongs_to": "unit_023" } ],
  "issue_ids": ["issue_003"], "confidence": 0.8
}
```
- `ordinal` = logical position (always set); `number_raw` = printed number or `null`.
- `placement`: `in_span` / `displaced` (segment lies in another unit's span, mirrored there as a `foreign_span`).
- A footnote's `takhrij`/`gharib` segments and its `anchor` share one `footnote_id`.

### `issues.json`
Grouped by type; every occurrence has `char_start`/`char_end` and, when inside a unit, `unit_id` (and a `note` with the evidence for grouped types). `confidence` = probability the issue is real. `needs_visual_check: true` where only the page image can decide.

---

## Rules for Arabic text

1. Preserve the text exactly: spelling, diacritics, classical vocabulary, digits as written.
2. Do not normalize or correct in the output; matching inside the engine ignores diacritics but offsets always point to the original.
3. Honorific ligatures (ﷺ، عز وجل، جل وعلا، رضي الله عنه) are often lost by OCR; a heading ending abruptly (`… الله عل`) is probably a lost ligature. Keep the OCR text as `title`, put the TOC form in `variants`, and let the reviewer check the page.
4. A rare classical word is not an OCR error. Flag an error only with a reason.

## No hallucination

Nothing from general knowledge goes into titles, text, metadata or the config. What general knowledge suggests (a probable full title, a likely missing introduction) may appear only as an issue candidate flagged for human review.

## Scalability

The engine works on the whole text in one pass (label array, linear scans), so it does not depend on chunking. For very large books the heading list in the config is the main effort: build it from the TOC and `discover` candidates, then let `config_headings_not_found` and the order checks tell you what is missing.

## Final principle

The database should reflect the book's own structure — and say clearly where the OCR makes that structure uncertain. Your understanding goes into the config and the overrides, where a human can read and check it; the code stays the same for every book.
