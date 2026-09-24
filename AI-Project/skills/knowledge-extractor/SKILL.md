---
name: knowledge-extractor
description: Extract knowledge objects (teachings, rulings, beliefs, principles, word meanings) from the units of an Arabic heritage book already structured by book-structure-analyzer, each tied to who says it and to an exact verbatim quote in the source. Use when a book's approved units.json must be turned into knowledge_objects.json for the heritage project, or when re-checking such an extraction.
---

# Knowledge Extractor

## Purpose

Turn each unit of a structured book (a hadith, a paragraph, a mas'ala) into **knowledge objects**:
one distinct teaching each, written in plain Arabic, saying **who** says it in the text and **proving**
it with a quote copied word for word from the source. Later stages (relations, articles, search) use
only these objects, so an object that is not in the text, or is given to the wrong speaker, spreads
everywhere. **Nothing is better than something invented.**

Input: the approved output of `book-structure-analyzer` for the book (`units.json`, `structure.json`)
and its source text. Output: `knowledge_objects.json`.

---

## How this skill is built — read first

```
knowledge-extractor/
├── SKILL.md
├── prompts/extract_unit.md     the fixed instructions for extracting ONE unit (prompt_version ke-v1)
├── scripts/
│   ├── prepare.py              makes a reading sheet per unit: its text in numbered pieces, each
│   │                           labelled with whose words it is (author / editor)
│   ├── check.py                checks the drafts, finds every quote in the source, computes offsets,
│   │                           pages and confidence, rejects anything that cannot be traced
│   └── common.py
├── templates/extraction_config.template.json
├── examples/<book>/extraction_config.json     finished configs: al-arbaeen-nawawiya, asul-elsona
└── tests/run_tests.py          the scripts' test (see Hard rule 6)
```

**You (the AI doing the extraction)** decide what the teachings are and who says them, and write one
draft per unit. **The scripts** do everything mechanical: they prepare the text, find your quotes,
compute exact positions and confidence, and reject what does not hold. You never write a position,
a page or a confidence.

### Hard rules

1. **Never edit** `scripts/`, `prompts/extract_unit.md` or `tests/`, and never write your own script
   for a book. If the scripts cannot express something, stop and tell the user.
2. **Never edit `knowledge_objects.json` by hand.** It is written by `check.py` only.
3. **Never add knowledge that is not in the text** — no names, dates, sources, meanings, rulings from
   memory. If the text says «سفيان», the object says «سفيان».
4. **Never make a rejection pass by changing the evidence.** A rejected object is fixed by reading the
   sheet again and quoting what is really there — or by dropping the object.
5. **Never give the editor's words to the author, or the author's words to the Prophet ﷺ.**
6. After any change to the scripts or the prompt (by the user or with the user's approval), run
   `python tests/run_tests.py`; every book must pass. A snapshot is replaced (`--approve`) only after
   the user has reviewed the differences.

---

## Workflow

Paths below use `<book>` = the book's work folder, e.g. `AI-Project/work/al-arbaeen-nawawiya`,
and `KE` = `AI-Project/skills/knowledge-extractor`.

### Phase A — the book's extraction config (then stop)

1. The book must have an **approved** structure build (`<book>/build/` or the folder the user names,
   with `units.json` and `structure.json`). Do not extract from an unapproved build.
2. If `KE/examples/<book>/extraction_config.json` exists, copy it to `<book>/extraction_config.json`
   and go to Phase B. Otherwise copy `KE/templates/extraction_config.template.json` and fill it:
   - `heading_by`: whose words the unit headings are — `editor` if the editor/publisher added them
     (find where the book says so, or compare with the TOC), `author` if they are the author's.
   - `types`: 3 to 8 kinds of knowledge this genre has (hadith collection: belief, worship, ruling,
     manners…; creed treatise: creed issue, refutation, method…), each with a one-sentence
     description and a short example **taken from this book**. Keep `word_meaning`.
   - `footnote_roles`: keep `takhrij` excluded; include `gharib` / `footnote`.
3. **Report to the user and stop:** the types (with their examples), `heading_by` and the evidence for
   it. Wait for approval.

### Phase B — a sample (then stop)

The user names the sample units (typically 5 units, spread over the book).

1. Sheets:
   ```bash
   python KE/scripts/prepare.py --source <book>/<source>.txt --build <book>/<approved build> \
          --config <book>/extraction_config.json --out <book>/extraction/sheets --units unit_002,unit_007,...
   ```
2. For **each** unit: read `KE/prompts/extract_unit.md`, then the unit's sheet
   `<book>/extraction/sheets/<unit_id>.md` and the config, and write
   `<book>/extraction/drafts/<unit_id>.json` exactly as the prompt says. One unit at a time.
3. Check:
   ```bash
   python KE/scripts/check.py --sheets <book>/extraction/sheets --drafts <book>/extraction/drafts \
          --config <book>/extraction_config.json --out <book>/extraction/knowledge_objects.json \
          --units unit_002,unit_007,...
   ```
   It prints `status: pass` or every rejection with its reason code. For each rejection, go back to
   the sheet and fix the **draft** (Hard rule 4), then check again. After two failed attempts on the
   same object, drop it and say so in the report.
4. **Report to the user and stop:** units done, objects per unit, every object you dropped and why,
   and anything in a sheet that looked wrong (text of one unit inside another, a footnote in the main
   text…) — that is a structure problem to report, not to work around. Wait for the review.

### Phase C — the whole book

Only after the user approves the sample: same as Phase B with `--units all` (prepare) and without
`--units` (check). Report the same way.

### Review checklist (for the reviewer, after Phase B and C)

- **Faithful:** for each object, read the quote in the sheet — does the statement say what the quote
  says, no more? Watch for an added cause, condition, or generalisation.
- **Speaker:** is `said_by` right? The Prophet's words vs a companion narrating vs the author's own
  comment vs a quoted scholar. `check.py` only checks that the name is in the text and that editor ⇔
  editor's pieces; it cannot tell who is speaking inside the author's text.
- **Complete:** is a clear teaching of the unit missing? Is one teaching split in two, or two merged?
- **Types:** is each object in the closest type?
- **inferred:** read every `inference_reason` — is the inference really in the words quoted?

---

## The reading sheet

`prepare.py` gives each unit's text as pieces `[p01]`, `[p02]`…:

| piece | contains | whose words |
|---|---|---|
| heading | the unit's heading and subtitle | per `heading_by` |
| main text | all of the unit's main text in order (footnote anchors removed) | the author's — it may report the words of others |
| source attribution | «رواه مسلم» … | the author's |
| footnote (n) | one of the editor's footnotes (roles per `footnote_roles`) | the editor's |

The `.json` twin of each sheet keeps where every piece came from in the source; `check.py` uses it to
turn a quote into exact positions.

## What check.py enforces

| code | rejected when |
|---|---|
| `E_QUOTE_NOT_FOUND` | the quote is not word for word in the piece named (diacritics, hamza/alef forms, ى/ي, ة/ه, punctuation and spacing are ignored — nothing else) |
| `E_QUOTE_SHORT` | fewer than `min_quote_letters` letters |
| `E_SPEAKER_ROLE` | `editor` with evidence outside the editor's pieces, or `author`/`prophet`/`quran`/`quoted` with evidence in the editor's pieces |
| `E_NAME_NOT_IN_TEXT` | `prophet`/`quoted` without a `name_in_text` found in the unit's author text |
| `E_TYPE`, `E_SPEAKER_KIND` | not in the config |
| `E_BASIS` | not `explicit`/`inferred`, or `inferred` without `inference_reason` |
| `E_STATEMENT`, `E_NO_EVIDENCE`, `E_PIECE` | empty/non-Arabic/too long statement, no evidence, unknown piece |
| `E_JSON`, `E_UNIT`, `E_PROMPT_VERSION`, `E_MODEL`, `E_EMPTY`, `E_TOO_MANY`, `E_MISSING_DRAFT` | a broken, misplaced, outdated, anonymous, empty or oversized draft, or no draft |

Rejected objects are listed in `knowledge_objects.json` → `rejected`, with their reasons; they never
disappear silently.

## knowledge_objects.json

```json
{
  "id": "ko_unit_004_02", "unit_id": "unit_004", "type": "refutation",
  "statement": "من قال إن القرآن مخلوق فهو مبتدع، ولم يُسمع أحد يقول ذلك.",
  "said_by": {"kind": "quoted", "name_in_text": "سفيان", "reported_by": "author"},
  "basis": "explicit",
  "evidence": [{"piece": "p02", "quote": "ومن قال مخلوق فهو مبتدع، لم نسمع أحداً يقول هذا",
                "char_start": 36319, "char_end": 36366, "role": "text", "page": 40, "page_end": 40,
                "parts": [{"char_start": 36319, "char_end": 36366, "role": "text", "page": 40}]}],
  "confidence": 0.75, "model_used": "…", "prompt_version": "ke-v1", "status": "experimental"
}
```
- `statement` is the extractor's wording — never quoted as the author's. The author's words are
  `evidence[].quote`, located exactly by `char_start`/`char_end` (and `parts`, when a quote crosses a
  page break or a footnote anchor).
- `confidence` (computed): 0.85 explicit / 0.6 inferred, capped by the unit's structure confidence.
- `status` is always `experimental` in this phase: nothing here has had a scholar's review.

## Not in this version

Entities and relations (persons, places, links between objects), splitting isnad from matn, grading,
anything from outside the text. Biographies, takhrij and manuscript variants in footnotes are skipped.
