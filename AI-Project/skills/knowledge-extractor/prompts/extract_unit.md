# Extraction instructions — prompt_version: ke-v1

You receive ONE unit of an Arabic heritage book as a reading sheet (`sheets/<unit_id>.md`) and the
book's `extraction_config.json`. You write ONE file: `drafts/<unit_id>.json`. Nothing else.

## What to extract
A **knowledge object** is one distinct teaching, ruling, belief, principle or word meaning that the
text itself states. One object per distinct teaching — usually 2 to 6 per unit, never more than the
config's `max_objects_per_unit`. Do not split one teaching into several objects; do not merge two.

## The sheet
The unit is given in numbered pieces: `[p01]`, `[p02]` … Each piece says whose words it is:
- **the AUTHOR's text** — the book itself. It may report the words of others (the Prophet ﷺ, a
  Qur'an verse, a quoted person); the author's own statements are the author's.
- **the EDITOR's words** — the editor's headings and footnotes. Never the author's.

## Each object
- `type` — one id from the config's `types` list. Read each type's description; pick the closest.
- `statement` — the teaching in one or two short sentences of plain modern Arabic, faithful to the
  text. No additions, no outside knowledge, no opinion. At most 300 characters.
- `said_by` — who says it IN THIS TEXT:
  - `{"kind": "prophet", "name_in_text": "<how the text names him, e.g. رسول الله>"}` — the Prophet's words
  - `{"kind": "quran"}` — a verse the text quotes
  - `{"kind": "quoted", "name_in_text": "<the name exactly as written>", "reported_by": "author"}` —
    someone the author quotes (a companion, a scholar…)
  - `{"kind": "author"}` — the author's own statement
  - `{"kind": "editor"}` — the editor's footnote (use only for word/term meanings, see below)
  `name_in_text` must be copied from the text as it is written there.
- `basis` — `"explicit"` if the text states it; `"inferred"` only if the text implies it clearly,
  and then add `"inference_reason"`: one sentence saying which words imply it.
- `evidence` — one or more `{"piece": "p02", "quote": "<words copied from that piece>"}`.
  The quote must be copied **word for word** from that piece: the checker ignores diacritics,
  hamza/alef forms and punctuation, nothing else. Quote the shortest passage that shows the
  teaching (at least 8 letters). Never write character offsets — the checker computes them.

## Editor's footnotes
Take from them **only** the meaning of a word or term (type `word_meaning`, said_by `editor`,
the quote from that footnote). Skip everything else in footnotes: sources and grading (takhrij),
manuscript variants (في ظ …), biographies, cross-references.

## Never
- Never add anything the text does not say — not a name, a date, a source, a meaning, a ruling.
  If the text says «سفيان», write «سفيان», even if you know who he is.
- Never attribute the editor's words to the author, or the author's words to the Prophet ﷺ.
- Never use a piece marked "the EDITOR's words" (a heading added by the editor, a footnote) as
  evidence of what the author, the Prophet ﷺ or a quoted person says.
- Never change a quote to make it pass. If you cannot quote it, do not claim it.

## If the unit has no knowledge
Write `"objects": []` and a one-sentence `"no_knowledge_reason"`.

## Output — exactly this shape, valid JSON, UTF-8
```json
{
  "unit_id": "unit_004",
  "model_used": "<your model name>",
  "prompt_version": "ke-v1",
  "no_knowledge_reason": null,
  "objects": [
    {
      "type": "<type id>",
      "statement": "<plain Arabic>",
      "said_by": {"kind": "quoted", "name_in_text": "<name>", "reported_by": "author"},
      "basis": "explicit",
      "evidence": [{"piece": "p02", "quote": "<copied words>"}]
    }
  ]
}
```
