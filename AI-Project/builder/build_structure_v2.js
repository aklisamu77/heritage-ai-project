'use strict';
// build_structure_v2.js - Phase B generator for book-structure-analyzer (v2 schema).
//
// Reads the OCR TXT + the derived page model (builder/pages.json, produced from
// the searchable PDF by builder/extract_pages.py) and emits the four output
// files with exact char offsets per the updated SKILL.md / scripts/validate.py:
//   book_profile.json, structure.json, units.json, issues.json
//
// Offset rules: Unicode code points into the UTF-8-decoded source, line endings
// as on disk (LF), char_end exclusive. All offsets computed by code.
//
// Usage:  node build_structure_v2.js <book-ocr.txt> <pages.json> <outdir>

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const INPUT = process.argv[2];
const PAGESJSON = process.argv[3];
const OUTDIR = process.argv[4];

const src = fs.readFileSync(INPUT, 'utf8');
const srcLen = src.length;
const PAGE_OBJS = JSON.parse(fs.readFileSync(PAGESJSON, 'utf8')).map(p => ({
  page_index: p.page_index, printed_page_number: p.printed_page_number,
  char_start: p.char_start, char_end: p.char_end,
  evidence: p.evidence, confidence: p.confidence,
}));
PAGE_OBJS.sort((a, b) => a.page_index - b.page_index);

// ---- line index -------------------------------------------------------------
const lines = [];
{
  let start = 0;
  for (;;) {
    const nl = src.indexOf('\n', start);
    if (nl === -1) { lines.push({ n: lines.length + 1, start, end: src.length }); break; }
    lines.push({ n: lines.length + 1, start, end: nl });
    start = nl + 1;
  }
}
const L = n => lines[n - 1];
const lineText = n => src.slice(L(n).start, L(n).end);

function pageIndexFor(off) {
  // containing page_index (1-based). Falls in [pages[0].char_start, end).
  let idx = 1;
  for (const p of PAGE_OBJS) if (off >= p.char_start) idx = p.page_index;
  return idx;
}
function pageStartFor(off) { return pageIndexFor(off); }
function pageEndFor(offEnd) { // exclusive end -> page containing the last char
  return pageIndexFor(Math.max(offEnd - 1, 0));
}

// ---- span helpers (internal cs/ce) -------------------------------------------
const spanSort = (a, b) => a.cs - b.cs || a.ce - b.ce;
function full(l) { return { cs: L(l).start, ce: L(l).end }; }
function spanLines(a, b) { return { cs: L(a).start, ce: L(b).end }; }
const pieceCache = new Map();
function _cut(l, m) {
  const key = l + '|' + m;
  if (!pieceCache.has(key)) {
    const t = lineText(l);
    const i = t.indexOf(m);
    pieceCache.set(key, { base: L(l).start, i, match: i >= 0 });
  }
  const c = pieceCache.get(key);
  if (!c.match) throw new Error('MARKER NOT FOUND: line ' + l + ' marker "' + m + '" in "' + lineText(l) + '"');
  return c;
}
// pieces(l, [{role, m, keep}]): role pieces across a line. keep: 'pre' ends at
// marker start, 'suf' runs marker->line end, 'm' is exactly the marker, null is
// rest of line. Used to build segments for the line.
function pieces(l, arr) {
  const t = lineText(l);
  const out = [];
  let from = 0;
  for (const p of arr) {
    if (p.m == null) {
      if (t.length > from) out.push({ role: p.role, cs: L(l).start + from, ce: L(l).start + t.length });
      break;
    }
    const c = _cut(l, p.m);
    const found = c.i;
    const cutEnd = p.keep === 'suf' ? t.length : p.keep === 'm' ? found + p.m.length : found;
    if (cutEnd > from) out.push({ role: p.role, cs: L(l).start + from, ce: L(l).start + cutEnd });
    from = cutEnd;
  }
  return out;
}
// segment of line l between marker fromM (inclusive) and marker toM (exclusive)
function sliceLine(role, l, fromM, toM) {
  const t = lineText(l);
  const a = _cut(l, fromM).i;
  const b = toM == null ? t.length : _cut(l, toM).i + toM.length;
  if (b <= a) return null;
  return { role, cs: L(l).start + a, ce: L(l).start + b };
}
function pre(role, l, m) { const p = pieces(l, [{ role, m, keep: 'pre' }])[0]; return p || null; }
function suf(role, l, m) { const p = pieces(l, [{ role, m, keep: 'suf' }])[0]; return p || null; }
function preSpan(l, m) { return pre('X', l, m); }

// ---- registries --------------------------------------------------------------
const units = [];
const noiseItems = [];
const FOOTNOTES = [];
const ISSUES = [];
const foreignList = [];
let noiseSeq = 0;
let fnSeq = 0;

function addNoise(type, s, unitId, note) {
  if (!s || s.ce <= s.cs) return null;
  const it = { id: 'noise_' + String(++noiseSeq).padStart(3, '0'), type,
    char_start: s.cs, char_end: s.ce };
  if (unitId) it.unit_id = unitId;
  if (note) it.note = note;
  noiseItems.push(it);
  return it;
}

function defFootnote(owner, marker, parts, placement, opts) {
  const id = 'f' + String(++fnSeq).padStart(3, '0');
  const segs = [];
  for (const p of parts) {
    const part = Array.isArray(p) ? { l: p[0], m: p[1], keep: p[2] || (p[1] ? 'pre' : null) }
      : typeof p === 'number' ? { l: p, m: null } : p;
    if (part.m) segs.push(pieces(part.l, [{ role: 'F', m: part.m, keep: part.keep || 'pre' }])[0] || null);
    else segs.push(full(part.l));
  }
  const opts2 = opts || {};
  const row = { id, owner, marker, placement, host_unit: opts2.host_unit, segs: segs.filter(Boolean), note: opts2.note };
  FOOTNOTES.push(row);
  return row;
}

function foreignIn(host, belongsTo, s, issueId, note) {
  if (!s || s.ce <= s.cs) return;
  foreignList.push({ host_unit: host, belongs_to: belongsTo, issue_id: issueId, cs: s.cs, ce: s.ce, note });
}
const fx = (owner, host) => ({ belongs_to: owner, host_unit: host });

// =============================================================================
// UNITS CONFIG (ported from the validated v1 analysis, adapted to the v2 schema)
// =============================================================================
// body lines are classified into isnad/matn later; explicit isnad/matn/attrib
// line lists override classification. extra holds inline piece-splits for lines
// that mix roles (markers etc).
const UNITS_CFG = [];

function defUnit(cfg) { UNITS_CFG.push(cfg); }

// ===== UNIT 1 =====
defUnit({
  id: 'unit_001', ordinal: 1, number_raw: null, l1: 37, l2: 57, base: 0.70,
  title: 'لا عمل إلا بنية', title_variants: [],
  heading: [[37, 'لا عمل إلا بنية', 'suf']],
  body: [38, 39, 40, 43, 44, 45, 46, 47, 48, 49],
  attrib: [50, 51, 52, 53],
  footnotes: [['unit_001', '(1)', [54, [55, 'M', 'pre']], 'in_span', { note: 'takhrij gloss' }]],
  anchors: [],
  noise: [
    ['displaced_fragment', preSpan(37, 'لا عمل إلا بنية'), '( + ۲۰۳ ) ٥٩٣٢٢٠٤ : residue before heading — edition/call-stamp artifact'],
    ['garbled_glyph', full(41), '"الله الرحمن الرحيم" — likely بسم الله الرحمن الرحيم with "بسم" ligature lost'],
    ['duplicate_fragment', full(42), 'duplicate of unit heading'],
    ['running_header', full(56), null],
    ['separator', full(57), '"-"'],
    ['garbled_glyph', suf('G', 55, 'M'), 'trailing M of "الفعل" is a garbled glyph'],
  ],
  issues: ['issue_004', 'issue_010', 'issue_012', 'issue_013', 'issue_014'],
});

// ===== UNIT 2 =====
defUnit({
  id: 'unit_002', ordinal: 2, number_raw: '۲', l1: 58, l2: 88, base: 0.80,
  title: 'مراتب الدين', title_variants: ['الإسلام والإيمان والإحسان'],
  heading: [58, 59],
  body: [60, 61, 62, 63, 64, 65, 66, 67, 68, 69, 70, 71, 72, 73, IT(74, 'أركان الإسلام', 'pre'), 75, 77, 78, 79, 80, 81, 83, 84],
  attrib: [86],
  footnotes: [['unit_002', '(1)', [[103], [104]], 'displaced', { host_unit: 'unit_003' }]],
  anchors: [[87, '(1)']],
  noise: [
    ['separator', full(76), '"» :" stray quote artifact'],
    ['separator', full(82), 'stray quote artifact'],
    ['separator', full(85), '"((" stray double-paren artifact'],
    ['separator', full(88), '"-"'],
  ],
  foreign: [{ belongs_to: 'unit_003', cs: ...0, ce: 0, note: 'replaced below', line: 74, marker: 'أركان الإسلام' }],
  issues: ['issue_010'],
});
// NOTE: unit_002 foreign handled specially below after heading tail known.

// ===== UNIT 3 =====
defUnit({
  id: 'unit_003', ordinal: 3, number_raw: null, l1: 89, l2: 107, base: 0.75,
  title: 'أركان الإسلام', title_variants: [],
  heading: [89],
  body: [90, 91, 92, 93, 94, 95, 96, 97, 98, 99, IT(100, '[ رواه', 'pre')],
  attrib: [[100, '[ رواه', 'suf']],
  footnotes: [['unit_003', '(٢)', [[107]], 'in_span', {}]],
  anchors: [[102, '(۲)']],
  noise: [
    ['separator', full(101), 'bullet'],
    ['separator', full(105), 'orphan quote mark'],
    ['separator', full(106), 'orphan paren'],
  ],
  foreign: [
    { belongs_to: 'unit_002', from: 103, to: 104, issue_id: 'issue_007', note: 'displaced footnote 1 of unit_002' },
  ],
  issues: [],
});
// ===== UNIT 4 =====
defUnit({
  id: 'unit_004', ordinal: 4, number_raw: null, l1: 108, l2: 132, base: 0.75,
  title: 'الخلق والأجل والرزق', title_variants: [],
  heading: [111],
  body: [114, 115, 116, 117, 118, 119, 120, 121, 122, 123, 124, 125, 126, IT(127, '[ رويه'.replace('رويه', 'رَوَاهُ'), 'pre')],
  attrib: [[127, 'رَوَاهُ', 'suf']],
  footnotes: [['unit_004', '(1)', [129, 130, 131], 'in_span', {}]],
  anchors: [[128, '(1)']],
  noise: [
    ['displaced_fragment', full(108), '".رضي" stray isnad/reordering fragment'],
    ['separator', full(109), '"-"'],
    ['page_number', full(110), '٤'],
    ['separator', full(112), '"-"'],
    ['running_header', full(113), null],
  ],
  issues: ['issue_004', 'issue_012'],
});
// ===== UNIT 5 =====
defUnit({
  id: 'unit_005', ordinal: 5, number_raw: '٥', l1: 133, l2: 140, base: 0.80,
  title: 'إنكار البدع', title_variants: [],
  heading: [133],
  body: [134, 135, 136, 137],
  attrib: [138, 139],
  footnotes: [
    ['unit_005', '(1)', [[149], [150]], 'displaced', { host_unit: 'unit_006' }],
    ['unit_005', '(٢)', [[152, 'الأربعين النووية', 'pre']], 'displaced', { host_unit: 'unit_006' }],
  ],
  anchors: [],
  noise: [['separator', full(140), '"-"']],
  issues: [],
});
// ===== UNIT 6 =====
defUnit({
  id: 'unit_006', ordinal: 6, number_raw: '٦', l1: 141, l2: 160, base: 0.80,
  title: 'الورع والإخلاص', title_variants: [],
  heading: [141],
  body: [142, 143, 144, 145, 146, 147, 148, 153, 154, 155, 157, 158, IT(159, '[رواه', 'pre')],
  attrib: [[159, '[رواه', 'suf']],
  footnotes: [
    ['unit_006', null, [[149, 'أخرجه', 'suf']], 'in_span', {}], // placeholder removed below
  ],
  anchors: [],
  noise: [
    ['separator', full(151), '"(( "'],
    ['separator', full(156), '","'],
    ['separator', full(160), 'bullet'],
    ['running_header', suf('R', 152, 'الأربعين النووية'), null],
  ],
  foreign: [
    { belongs_to: 'unit_005', from: 149, to: 150, issue_id: 'issue_007', note: 'displaced footnote 1 of unit_005' },
    { belongs_to: 'unit_005', marker: 'الأربعين النووية', line: 152, issue_id: 'issue_007', note: 'displaced footnote 2 of unit_005' },
  ],
  issues: [],
});
// ===== UNIT 7 =====
defUnit({
  id: 'unit_007', ordinal: 7, number_raw: '۷', l1: 161, l2: 170, base: 0.70,
  title: 'النصح من أصول الإسلام', title_variants: [],
  heading: [161],
  body: [163, 164, 165, 166, 167, 168, IT(169, 'رَوَاهُ', 'pre')],
  attrib: [[169, 'رَوَاهُ', 'suf']],
  footnotes: [],
  anchors: [],
  noise: [
    ['separator', full(170), '"-"'],
    ['unmatched_anchor', full(162), '(1) anchor with NO footnote text anywhere — missing footnote'],
  ],
  issues: ['issue_004', 'issue_009'],
});
// ===== UNIT 8 =====
defUnit({
  id: 'unit_008', ordinal: 8, number_raw: null, l1: 171, l2: 182, base: 0.75,
  title: 'حرمة دم المسلم وماله', title_variants: [],
  heading: [171],
  body: [173, 174, 175, 179, 180, 181, IT(182, '[رواه', 'pre')],
  attrib: [[182, '[رواه', 'suf']],
  footnotes: [
    ['unit_006', '(1)', [[176], [177]], 'displaced', { host_unit: 'unit_008', note: 'glosses الحمى/يرتع — belongs to unit_006' }],
    ['unit_008', '(٢)', [[178, 'الكسب', 'pre']], 'in_span', {}],
  ],
  anchors: [[172, '(۲)']],
  noise: [
    ['separator', suf('S', 178, 'الكسب'), 'tail = displaced heading of unit_010'],
  ],
  foreign: [
    { belongs_to: 'unit_006', from: 176, to: 177, issue_id: 'issue_007', note: 'displaced footnote 1 of unit_006' },
  ],
  issues: ['issue_011'],
});
// ===== UNIT 9 =====
defUnit({
  id: 'unit_009', ordinal: 9, number_raw: '۹', l1: 183, l2: 192, base: 0.80,
  title: 'الطاعة وعدم التعنت سبيل النجاة', title_variants: [],
  heading: [183],
  body: [185, 186, 187, 188, 189, 190, IT(191, 'رَوَاهُ', 'pre')],
  attrib: [[191, 'رَوَاهُ', 'suf']],
  footnotes: [
    ['unit_009', '(1)', [[201]], 'displaced', { host_unit: 'unit_010' }],
    ['unit_009', '(٢)', [[203, 'الأربعين النووية', 'pre']], 'displaced', { host_unit: 'unit_010' }],
  ],
  anchors: [[184, '(1)'], [192, '(۲)']],
  noise: [],
  issues: [],
});
// ===== UNIT 10 =====
defUnit({
  id: 'unit_010', ordinal: 10, number_raw: null, l1: 193, l2: 215, base: 0.75,
  title: 'الكسب الحلال سبب إجابة الدعاء', title_variants: [],
  heading: [193],
  body: [196, 197, 198, 204, 205, 206, 207, 208, 209, 210, 211, IT(212, 'رَواهُ', 'pre')],
  attrib: [[212, 'رَواهُ', 'suf']],
  footnotes: [
    ['unit_010', '(٢)', [[202]], 'in_span', {}],
    ['unit_009', '(1)', [[201]], 'displaced', { host_unit: 'unit_010' }],
    ['unit_009', '(٢)', [[203, 'الأربعين النووية', 'pre']], 'displaced', { host_unit: 'unit_010' }],
  ],
  anchors: [[215, '(1)']],
  noise: [
    ['separator', full(194), '"-"'],
    ['separator', full(195), '"-"'],
    ['separator', full(199), 'bullet'],
    ['separator', full(200), '"-"'],
    ['page_number', full(213), '۱۱'],
    ['separator', full(214), '"-"'],
    ['running_header', suf('R', 203, 'الأربعين النووية'), null],
  ],
  foreign: [
    { belongs_to: 'unit_009', from: 201, to: 201, issue_id: 'issue_007', note: 'displaced footnote 1 of unit_009' },
    { belongs_to: 'unit_009', marker: 'الأربعين النووية', line: 203, issue_id: 'issue_007', note: 'displaced footnote 2 of unit_009' },
  ],
  issues: ['issue_004'],
});
// ===== UNIT 11 =====
defUnit({
  id: 'unit_011', ordinal: 11, number_raw: null, l1: 216, l2: 226, base: 0.75,
  title: 'البعد عن الشبهات', title_variants: [],
  heading: [216],
  body: [217, 218, 219, 220, 223],
  attrib: [224],
  footnotes: [['unit_010', '(1)', [[221], [222, 'أخوة', 'pre']], 'displaced', { host_unit: 'unit_011', note: 'glosses أشعث (unit_010)' }]],
  anchors: [],
  noise: [
    ['page_number', full(225), '۱۲'],
    ['separator', full(226), '"-"'],
    ['separator', suf('S', 222, 'أخوة'), 'tail = displaced heading of unit_013'],
  ],
  foreign: [
    { belongs_to: 'unit_010', from: 221, to: 222, issue_id: 'issue_007', note: 'displaced footnote 1 of unit_010' },
  ],
  issues: ['issue_004', 'issue_011'],
});
// ===== UNIT 12 =====
defUnit({
  id: 'unit_012', ordinal: 12, number_raw: null, l1: 227, l2: 233, base: 0.75,
  title: 'الاشتغال بما يفيد', title_variants: [],
  heading: [227],
  body: [228, 229, 230, IT(231, '[ حديث', 'pre')],
  attrib: [],
  footnotes: [['unit_012', '(٢)', [[245]], 'displaced', { host_unit: 'unit_013', note: 'takhrij al-Zuhd — belongs to unit_012' }]],
  anchors: [[231, '(۲)']],
  noise: [
    ['page_number', full(232), '۱۳'],
    ['separator', full(233), '"-"'],
  ],
  issues: ['issue_004', 'issue_009'],
  // line 231: attribution tail '[ حديثٌ حَسَنٌ ... هَكَذَا ]' then anchor (۲)
  extra: [
    { builder: (u) => u.pushSeg({ role: 'source_attribution', cs: (lineStart(231) + lineText(231).indexOf('[ حديث')), ce: (lineStart(231) + lineText(231).indexOf('(۲')) }) },
  ],
});
// ===== UNIT 13 =====
defUnit({
  id: 'unit_013', ordinal: 13, number_raw: null, l1: 234, l2: 247, base: 0.75,
  title: 'أخوة الإيمان والإسلام', title_variants: [],
  heading: [234],
  body: [236, 237, 238, 239, 240],
  attrib: [241],
  footnotes: [
    ['unit_011', '(1)', [[243], [244]], 'displaced', { host_unit: 'unit_013', note: 'glosses ما يريبك — belongs to unit_011' }],
    ['unit_012', '(٢)', [[245]], 'displaced', { host_unit: 'unit_013' }],
    ['unit_013', '(۳)', [[246, '.١٤', 'pre']], 'in_span', {}],
  ],
  anchors: [],
  noise: [
    ['separator', full(235), '"-"'],
    ['separator', full(242), 'bullet'],
    ['running_header', full(247), null],
    ['page_number', suf('PN', 246, '.١٤'), '١٤'],
  ],
  foreign: [
    { belongs_to: 'unit_011', from: 243, to: 244, issue_id: 'issue_007', note: 'displaced footnote 1 of unit_011' },
    { belongs_to: 'unit_012', marker: '(', line: 245, issue_id: 'issue_007', note: 'displaced footnote of unit_012' },
  ],
  issues: ['issue_004'],
});
// ===== UNIT 14 =====
defUnit({
  id: 'unit_014', ordinal: 14, number_raw: null, l1: 248, l2: 255, base: 0.75,
  title: 'حرمة دم المسلم ومتى تهدر ؟', title_variants: [],
  heading: [248],
  body: [250, 251, 253, 254],
  attrib: [],
  footnotes: [['unit_014', '(1)', [[265], [266], [267]], 'displaced', { host_unit: 'unit_015', note: 'glosses امرئ مسلم — belongs to unit_014' }]],
  anchors: [],
  noise: [
    ['separator', full(249), '"-"'],
    ['separator', full(252), '"-"'],
    ['separator', { cs: lineStart(255) + lineText(255).indexOf(']') + 1, ce: lineEnd(255) }, 'trailing " )"'],
  ],
  issues: ['issue_004'],
  extra: [
    { builder: (u) => u.pushSeg({ role: 'source_attribution', cs: (lineStart(255) + lineText(255).indexOf('[')), ce: (lineStart(255) + lineText(255).indexOf(']') + 1) }) },
  ],
});
// ===== UNIT 15 =====
defUnit({
  id: 'unit_015', ordinal: 15, number_raw: '١٥', l1: 256, l2: 272, base: 0.75,
  title: 'حق الضيف والجار', title_variants: [],
  heading: [256],
  body: [257, 258, 259, 260, 261, IT(262, '[ رواه', 'pre')],
  attrib: [[262, '[ رواه', 'suf']],
  footnotes: [
    ['unit_015', '(٢)', [[269], [270]], 'in_span', {}],
    ['unit_014', '(1)', [[265], [266], [267]], 'displaced', { host_unit: 'unit_015' }],
  ],
  anchors: [[263, '(۲)']],
  noise: [
    ['displaced_fragment', full(264), '"القسامة (٢٥)" — marginal fragment about unit_014 (القسامة/الديات)'],
    ['separator', full(268), 'bullet'],
  ],
  foreign: [
    { belongs_to: 'unit_014', from: 265, to: 267, issue_id: 'issue_007', note: 'displaced footnote 1 of unit_014' },
  ],
  issues: ['issue_009', 'issue_016'],
});
// ===== UNIT 16 =====
defUnit({
  id: 'unit_016', ordinal: 16, number_raw: '١٦', l1: 273, l2: 279, base: 0.80,
  title: 'لا تغضب ولك الجنة', title_variants: [],
  heading: [273],
  body: [274, 275],
  attrib: [[277, '[ رواه', 'suf']],
  footnotes: [['unit_016', '(1)', [[297]], 'displaced', { host_unit: 'unit_018' }]],
  anchors: [[277, '(۱)']],
  noise: [
    ['separator', full(276), '"» :"'],
    ['separator', full(278), '"-"'],
    ['page_number', full(279), '۱۷'],
  ],
  issues: [],
  extra: [
    { builder: (u) => u.pushSeg({ role: 'matn', cs: lineStart(277), ce: (lineStart(277) + lineText(277).indexOf('[ رواه')) }) },
  ],
});
// ===== UNIT 17 =====
defUnit({
  id: 'unit_017', ordinal: 17, number_raw: null, l1: 280, l2: 289, base: 0.75,
  title: 'الإحسان', title_variants: [],
  heading: [280],
  body: [281, 283, 284, 286, 287, IT(288, 'رَوَاهُ', 'pre')],
  attrib: [[288, 'رَوَاهُ', 'suf']],
  footnotes: [['unit_017', '(٢)', [[298, '.١٤', 'pre']], 'displaced', { host_unit: 'unit_018' }]],
  anchors: [[289, '(۲)']],
  noise: [
    ['separator', full(282), 'bullet'],
    ['separator', full(285), 'stray quote'],
  ],
  issues: ['issue_004'],
});
// ===== UNIT 18 =====
defUnit({
  id: 'unit_018', ordinal: 18, number_raw: null, l1: 290, l2: 307, base: 0.80,
  title: 'تقوى الله وحسن الخلق', title_variants: [],
  heading: [290],
  body: [293, 294, 295, 296, 299, 300, 302, 303, IT(304, 'رَواهُ', 'pre')],
  attrib: [[304, 'رَواهُ', 'suf'], [305]],
  footnotes: [
    ['unit_016', '(1)', [[297]], 'displaced', { host_unit: 'unit_018' }],
    ['unit_017', '(٢)', [[298, '.١٤', 'pre']], 'displaced', { host_unit: 'unit_018' }],
  ],
  anchors: [],
  noise: [
    ['page_number', full(291), '۱۸'],
    ['separator', full(292), '"-"'],
    ['running_header', full(301), null],
    ['page_number', suf('PN', 298, '.١٤'), '١٤'],
    ['page_number', full(306), '۱۹'],
    ['separator', full(307), '"-"'],
  ],
  foreign: [
    { belongs_to: 'unit_016', marker: '(', line: 297, issue_id: 'issue_007', note: 'displaced footnote 1 of unit_016' },
    { belongs_to: 'unit_017', marker: '.١٤', line: 298, issue_id: 'issue_007', note: 'displaced footnote of unit_017' },
  ],
  issues: ['issue_004'],
});
// ===== UNIT 19 =====
defUnit({
  id: 'unit_019', ordinal: 19, number_raw: null, l1: 308, l2: 329, base: 0.80,
  title: 'عون الله تعالى وحفظه', title_variants: [],
  heading: [308],
  body: [309, 311, 312, 313, 314, 315, 316, 317, 318, 319, IT(323, '[', 'pre'), 325, 326, 327, 328],
  attrib: [],
  footnotes: [
    ['unit_019', '(1)', [[320], [321, 'فضيلة', 'pre']], 'in_span', { note: 'glosses تمحها' }],
    ['unit_019', '(1)', [[336], [337]], 'displaced', { host_unit: 'unit_020', note: 'glosses رفعت الأقلام — second footnote printed with marker (1)' }],
    ['unit_019', '(٢)', [[338]], 'displaced', { host_unit: 'unit_020' }],
  ],
  anchors: [[310, '(1)'], [324, '(١']],
  noise: [
    ['separator', full(322), 'stray bullet artifact'],
    ['separator', full(329), '"-"'],
    ['displaced_heading', suf('D', 321, 'فضيلة'), 'tail = heading of unit_020'],
  ],
  issues: ['issue_004'],
  extra: [
    { builder: (u) => u.pushSeg({ role: 'source_attribution', cs: (lineStart(323) + lineText(323).indexOf('[')), ce: lineEnd(323) }) },
    { builder: (u) => u.pushSeg({ role: 'source_attribution', cs: lineStart(324), ce: (lineStart(324) + lineText(324).indexOf('(١')) }) },
    { builder: (u) => u.pushSeg({ role: 'matn', cs: (lineStart(324) + lineText(324).indexOf('(١') + '(١'.length), ce: lineEnd(324) }) },
  ],
});
// ===== UNIT 20 =====
defUnit({
  id: 'unit_020', ordinal: 20, number_raw: null, l1: 330, l2: 341, base: 0.75,
  title: 'فضيلة الحياء', title_variants: [],
  heading: [330],
  body: [331, 332, 333, IT(334, '[ رواه', 'pre')],
  attrib: [[334, '[ رواه', 'suf']],
  footnotes: [
    ['unit_020', '(۳)', [[339], [340, 'ܙ', 'pre']], 'in_span', {}],
    ['unit_019', '(1)', [[336], [337]], 'displaced', { host_unit: 'unit_020' }],
    ['unit_019', '(٢)', [[338]], 'displaced', { host_unit: 'unit_020' }],
  ],
  anchors: [[335, '(۳)']],
  noise: [
    ['page_number', full(341), '۲۱'],
    ['garbled_glyph', suf('G', 340, 'ܙ'), 'trailing glyph ܙ'],
  ],
  foreign: [
    { belongs_to: 'unit_019', from: 336, to: 337, issue_id: 'issue_007', note: 'displaced footnote 1 of unit_019' },
    { belongs_to: 'unit_019', from: 338, to: 338, issue_id: 'issue_007', note: 'displaced footnote 2 of unit_019' },
  ],
  issues: ['issue_004'],
});
// ===== UNIT 21 =====
defUnit({
  id: 'unit_021', ordinal: 21, number_raw: '٢١', l1: 342, l2: 349, base: 0.80,
  title: 'الاستقامة لب الإسلام', title_variants: [],
  heading: [342],
  body: [345, 346, 347, IT(348, 'رَوَاهُ', 'pre')],
  attrib: [[348, 'رَوَاهُ', 'suf']],
  footnotes: [],
  anchors: [],
  noise: [
    ['running_header', full(343), null],
    ['separator', full(344), '"-"'],
    ['separator', full(349), '"-"'],
  ],
  issues: ['issue_010'],
});
// ===== UNIT 22 =====
defUnit({
  id: 'unit_022', ordinal: 22, number_raw: '۲۲', l1: 350, l2: 363, base: 0.85,
  title: 'طريق الجنة', title_variants: [],
  heading: [350],
  body: [352, 353, 354, 355, IT(356, '[ رَوَاهُ', 'pre'), IT(357, 'مَعْنَى', 'pre')],
  attrib: [],
  editorial: [],
  footnotes: [
    ['unit_022', '(1)', [[358]], 'in_span', {}],
    ['unit_022', '(٢)', [[359], [360, 'فضل الله على', 'pre']], 'in_span', {}],
  ],
  anchors: [[351, '(1)'], [357, '(۲)']],
  noise: [
    ['displaced_heading', suf('D', 360, 'فضل الله على'), 'tail = heading of unit_024'],
    ['separator', full(362), '"-"'],
    ['separator', full(363), '"-"'],
    ['ambiguous_number', full(361), '۲۳ — page-top token, interpreted as unit_023 number'],
  ],
  foreign: [{ belongs_to: 'unit_023', from: 361, to: 361, issue_id: 'issue_003', note: 'page-top number token interpreted as unit_023 number' }],
  issues: ['issue_003'],
  extra: [
    { builder: (u) => u.pushSeg({ role: 'source_attribution', cs: (lineStart(356) + lineText(356).indexOf('[ رَوَاهُ')), ce: lineEnd(356) }) },
    { builder: (u) => u.pushSeg({ role: 'source_attribution', cs: lineStart(357), ce: (lineStart(357) + lineText(357).indexOf('مَعْنَى')) }) },
    { builder: (u) => u.pushSeg({ role: 'editorial_note', cs: (lineStart(357) + lineText(357).indexOf('مَعْنَى')), ce: (lineStart(357) + lineText(357).indexOf('(۲')) }) },
  ],
});
// ===== UNIT 23 =====
defUnit({
  id: 'unit_023', ordinal: 23, number_raw: '۲۳', l1: 364, l2: 374, base: 0.75,
  title: 'جوامع الخير', title_variants: [],
  heading: [364],
  body: [366, 368, 369, 370, 372, 373, IT(374, 'رَوَاهُ', 'pre')],
  attrib: [[374, 'رَوَاهُ', 'suf']],
  footnotes: [['unit_023', '(1)', [[383], [384]], 'displaced', { host_unit: 'unit_024', note: 'glosses شطر (unit_023)' }]],
  anchors: [],
  noise: [
    ['page_number', full(365), '۱۷'],
    ['separator', full(367), '"-"'],
    ['separator', full(371), '"-"'],
  ],
  issues: ['issue_003'],
});
// ===== UNIT 24 =====
defUnit({
  id: 'unit_024', ordinal: 24, number_raw: '٢٤', l1: 375, l2: 406, base: 0.75,
  title: 'فضل الله على', title_variants: ['فضل الله وعل (TOC)'],
  heading: [375],
  body: [379, 380, 381, 382, 387, 388, 389, 390, 391, 392, 393, 394, 395, 396, 397, 398, 399, 400, 401, IT(402, 'رَوَاهُ', 'pre')],
  attrib: [[402, 'رَوَاهُ', 'suf']],
  footnotes: [
    ['unit_023', '(1)', [[383], [384]], 'displaced', { host_unit: 'unit_024' }],
    ['unit_024', '(1)', [[404], [405, 'كثرة', 'pre']], 'in_span', {}],
  ],
  anchors: [[378, '(1)'], [403, '(1)']],
  noise: [
    ['separator', full(376), '"-"'],
    ['ambiguous_number', full(377), '٢٤ — page-top token, interpreted as unit number'],
    ['running_header', full(386), null],
    ['page_number', full(385), '•۱۸'],
    ['page_number', full(406), '۲۰'],
    ['displaced_heading', suf('D', 405, 'كثرة'), 'tail = heading of unit_026'],
  ],
  foreign: [
    { belongs_to: 'unit_023', from: 383, to: 384, issue_id: 'issue_007', note: 'displaced footnote 1 of unit_023' },
    { belongs_to: 'unit_026', from: 405, to: 405, issue_id: 'issue_011', note: 'displaced heading tail' },
  ],
  issues: ['issue_003', 'issue_017', 'issue_014'],
});
// ===== UNIT 25 =====
defUnit({
  id: 'unit_025', ordinal: 25, number_raw: null, l1: 407, l2: 422, base: 0.75,
  title: 'فضل الذكر', title_variants: [],
  heading: [407],
  body: [408, 410, 411, 412, 413, 414, 415, 416, 417, 418, 419, 420, IT(421, 'رَوَاهُ', 'pre')],
  attrib: [[421, 'رَوَاهُ', 'suf'], [422]],
  footnotes: [['unit_025', '(1)', [[428]], 'displaced', { host_unit: 'unit_026', note: 'glosses بضع — unanchored in its owner; hosted in unit_026 span' }]],
  anchors: [],
  noise: [['page_number', full(409), '۱۹']],
  issues: ['issue_004', 'issue_014'],
});
// ===== UNIT 26 =====
defUnit({
  id: 'unit_026', ordinal: 26, number_raw: '٢٦', l1: 423, l2: 435, base: 0.80,
  title: 'كثرة طرق الخير', title_variants: [],
  heading: [423],
  body: [425, 426, 427, 430, 431, 432, 433, 434, IT(435, '[ رواه', 'pre')],
  attrib: [[435, '[ رواه', 'suf']],
  footnotes: [['unit_025', '(1)', [[428]], 'displaced', { host_unit: 'unit_026' }]],
  anchors: [[424, '(۱)']],
  noise: [['running_header', suf('R', 429, 'الأربعين النووية'), 'prefix junk is OCR residue of running header']],
  foreign: [
    { belongs_to: 'unit_025', marker: '(', line: 428, issue_id: 'issue_007', note: 'displaced footnote 1 of unit_025' },
  ],
  issues: [],
});
// ===== UNIT 27 =====
defUnit({
  id: 'unit_027', ordinal: 27, number_raw: '۲۷', l1: 436, l2: 460, base: 0.80,
  title: 'البر والإثم', title_variants: [],
  heading: [436],
  body: [438, 439, IT(440, 'رَوَاهُ', 'pre'), 442, 443, 444, 445, 446, 453, 454, 455],
  attrib: [[456, '[ حديث حَسَنٌ', 'suf'], [457]],
  footnotes: [
    ['unit_026', '(1)', [[447]], 'displaced', { host_unit: 'unit_027' }],
    ['unit_027', '(٢)', [[448], [449], [450, 'الطاعة', 'pre']], 'in_span', {}],
  ],
  anchors: [[437, '(1)'], [460, '(۱)']],
  noise: [
    ['separator', full(441), 'stray "]" closing the first report attribution'],
    ['page_number', full(451), '۲۱'],
    ['separator', full(452), '"(( "'],
    ['separator', full(458), '"-"'],
    ['ambiguous_number', full(459), '۲۸ — page-top token, interpreted as unit_028 number'],
    ['displaced_heading', suf('D', 450, 'الطاعة'), 'tail = heading of unit_028'],
  ],
  foreign: [
    { belongs_to: 'unit_026', marker: '(', line: 447, issue_id: 'issue_007', note: 'displaced footnote 1 of unit_026' },
    { belongs_to: 'unit_028', from: 459, to: 459, issue_id: 'issue_003', note: 'page-top number token interpreted as unit_028 number' },
  ],
  issues: ['issue_018'],
});
// ===== UNIT 28 =====
defUnit({
  id: 'unit_028', ordinal: 28, number_raw: '۲۸', l1: 461, l2: 479, base: 0.85,
  title: 'الطاعة والتزام السنة', title_variants: [],
  heading: [461],
  body: [462, 463, 464, 465, 466, 467, 468, 469, 470, 471, 477, IT(478, 'رَواهُ', 'pre')],
  attrib: [[478, 'رَواهُ', 'suf']],
  footnotes: [['unit_028', '(۱)', [[473], [474], [475, '۲۲', 'pre']], 'in_span', {}]],
  anchors: [[479, '(1)']],
  noise: [
    ['separator', full(472), '")) "'],
    ['running_header', full(476), null],
    ['page_number', suf('PN', 475, '۲۲'), '۲۲'],
  ],
  issues: ['issue_003'],
});
// ===== UNIT 29 =====
defUnit({
  id: 'unit_029', ordinal: 29, number_raw: '۲۹', l1: 480, l2: 511, base: 0.85,
  title: 'ذروة الإسلام وعموده', title_variants: [],
  heading: [480],
  body: [481, 482, 483, 484, 485, 486, 487, 488, 489, 490, 491, 497, 498, 499, 500, 501, 502, 503, 504, 505],
  attrib: [511],
  footnotes: [['unit_029', '(1)', [[492], [493], [494], [495, 'الوقوف', 'pre']], 'in_span', {}]],
  anchors: [],
  noise: [
    ['displaced_heading', suf('D', 495, 'الوقوف'), 'tail = heading of unit_030'],
    ['separator', full(496), '"(( "'],
    ['displaced_fragment', full(506), '"السِنَتِهِمْ" stray fragment'],
    ['displaced_fragment', full(507), '"رضی" stray fragment'],
    ['separator', full(508), '"(( "'],
    ['separator', full(509), '"-"'],
    ['separator', full(510), '"-"'],
  ],
  issues: ['issue_012'],
});
// ===== UNIT 30 =====
defUnit({
  id: 'unit_030', ordinal: 30, number_raw: '٣٠', l1: 512, l2: 525, base: 0.85,
  title: 'الوقوف عند حدود الشرع', title_variants: [],
  heading: [512],
  body: [514, 515, 516, 517, 518],
  attrib: [522],
  footnotes: [['unit_030', '(1)', [[519], [520, '٢٤', 'pre']], 'in_span', {}]],
  anchors: [[513, '(1)'], [525, '(1)']],
  noise: [
    ['page_number', suf('PN', 520, '٢٤'), '٢٤'],
    ['running_header', full(521), null],
    ['ambiguous_number', full(523), '۳۱ — page-top token, interpreted as unit_031 number'],
    ['separator', full(524), '"-"'],
  ],
  foreign: [{ belongs_to: 'unit_031', from: 523, to: 523, issue_id: 'issue_003', note: 'page-top number token interpreted as unit_031 number' }],
  issues: ['issue_003'],
});
// ===== UNIT 31 =====
defUnit({
  id: 'unit_031', ordinal: 31, number_raw: '۳۱', l1: 526, l2: 535, base: 0.80,
  title: 'الزهد وثمرته', title_variants: [],
  heading: [526],
  body: [527, 529, 530, 531, 532, 533, 534, IT(535, '[ حديثٌ', 'pre')],
  attrib: [[535, '[ حديثٌ', 'suf']],
  footnotes: [],
  anchors: [],
  noise: [['separator', full(528), '"-"']],
  issues: ['issue_003'],
});
// ===== UNIT 32 =====
defUnit({
  id: 'unit_032', ordinal: 32, number_raw: '۳۲', l1: 536, l2: 551, base: 0.85,
  title: 'لا ضرر ولا ضرار', title_variants: [],
  heading: [536],
  body: [539, 540, IT(541, '[ حديث', 'pre')],
  attrib: [[541, '[ حديث', 'suf']],
  editorial: [548, 549, 550],
  footnotes: [
    ['unit_032', '(١)', [[542]], 'in_span', {}],
    ['unit_032', '(٢)', [[543]], 'in_span', {}],
    ['unit_032', '(۳)', [[546, 'إزالة', 'pre']], 'in_span', {}],
  ],
  anchors: [[537, '(۲)'], [545, '(۳)']],
  noise: [
    ['separator', full(538), '"-"'],
    ['separator', full(544), 'bullet'],
    ['page_number', full(547), '۲۵'],
    ['separator', full(551), 'bullet'],
    ['displaced_heading', suf('D', 546, 'إزالة'), 'tail = heading of unit_034'],
  ],
  issues: [],
});
// ===== UNIT 33 =====
defUnit({
  id: 'unit_033', ordinal: 33, number_raw: '۳۳', l1: 552, l2: 559, base: 0.90,
  title: 'أسس القضاء في الإسلام', title_variants: [],
  heading: [552],
  body: [553, 554, 555, IT(556, '[ حديثٌ', 'pre')],
  attrib: [[556, '[ حديثٌ', 'suf'], [557]],
  footnotes: [],
  anchors: [[559, '(1)']],
  noise: [
    ['separator', full(558), '"(( "'],
  ],
  issues: ['issue_009'],
});
// ===== UNIT 34 =====
defUnit({
  id: 'unit_034', ordinal: 34, number_raw: '34', l1: 560, l2: 571, base: 0.90,
  title: 'إزالة المنكر فريضة إسلامية محكمة', title_variants: [],
  heading: [560],
  body: [561, 562, 563, 564, 565, 566, IT(569, 'رَواهُ', 'pre')],
  attrib: [[569, 'رَواهُ', 'suf']],
  footnotes: [['unit_034', '(1)', [[567, '.٢٦', 'pre']], 'in_span', {}]],
  anchors: [[571, '(1)']],
  noise: [
    ['running_header', full(568), null],
    ['ambiguous_number', full(570), '۳۵ — page-top token, interpreted as unit_035 number'],
    ['page_number', suf('PN', 567, '.٢٦'), '٢٦'],
  ],
  foreign: [{ belongs_to: 'unit_035', from: 570, to: 570, issue_id: 'issue_003', note: 'page-top number token interpreted as unit_035 number' }],
  issues: ['issue_003'],
});
// ===== UNIT 35 =====
defUnit({
  id: 'unit_035', ordinal: 35, number_raw: '۳۵', l1: 572, l2: 584, base: 0.80,
  title: 'حقوق الأخوة في الإسلام', title_variants: [],
  heading: [572],
  body: [573, 574, 575, 577, 578, 579, 580, 581],
  attrib: [],
  footnotes: [
    ['unit_035', '(1)', [[589]], 'displaced', { host_unit: 'unit_036' }],
    ['unit_035', '(٢)', [[591], [592, 'عظيم', 'pre']], 'displaced', { host_unit: 'unit_036' }],
  ],
  anchors: [],
  noise: [
    ['separator', full(576), 'stray quote'],
    ['ambiguous_number', full(583), '٣٦ — page-top token, interpreted as unit_036 number'],
    ['separator', full(584), '"-"'],
  ],
  foreign: [{ belongs_to: 'unit_036', from: 583, to: 583, issue_id: 'issue_003', note: 'page-top number token interpreted as unit_036 number' }],
  issues: ['issue_003', 'issue_010'],
  extra: [
    { builder: (u) => u.pushSeg({ role: 'matn', cs: lineStart(582), ce: (lineStart(582) + lineText(582).indexOf('[رَوَاهُ')) }) },
    { builder: (u) => u.pushSeg({ role: 'source_attribution', cs: (lineStart(582) + lineText(582).indexOf('[رَوَاهُ')), ce: (lineStart(582) + lineText(582).indexOf('(۲')) }) },
  ],
});
// ===== UNIT 36 =====
defUnit({
  id: 'unit_036', ordinal: 36, number_raw: '٣٦', l1: 585, l2: 605, base: 0.80,
  title: 'التعاون والعلم والعمل', title_variants: [],
  heading: [585],
  body: [587, 588, 595, 596, 598, 599, 600, 601, 602, 603, 604, IT(605, 'رَوَاهُ', 'pre')],
  attrib: [[605, 'رَوَاهُ', 'suf']],
  footnotes: [
    ['unit_035', '(1)', [[589]], 'displaced', { host_unit: 'unit_036' }],
    ['unit_035', '(٢)', [[591], [592, 'عظيم', 'pre']], 'displaced', { host_unit: 'unit_036' }],
  ],
  anchors: [],
  noise: [
    ['separator', full(586), '"-"'],
    ['separator', full(590), 'bullet'],
    ['separator', full(593), '","'],
    ['separator', full(597), 'stray "؛"'],
    ['page_number', full(594), '۲۷'],
    ['displaced_heading', suf('D', 592, 'عظيم'), 'tail = heading of unit_037'],
  ],
  foreign: [
    { belongs_to: 'unit_035', marker: '(', line: 589, issue_id: 'issue_007', note: 'displaced footnote 1 of unit_035' },
    { belongs_to: 'unit_035', marker: '(', line: 591, issue_id: 'issue_007', note: 'displaced footnote 2 of unit_035' },
  ],
  issues: ['issue_003'],
});
// ===== UNIT 37 =====
defUnit({
  id: 'unit_037', ordinal: 37, number_raw: '۳۷', l1: 606, l2: 638, base: 0.85,
  title: 'عظيم لطف الله وفضله', title_variants: [],
  heading: [606],
  body: [608, 609, 610, 616, 617, 618, 619, 620],
  attrib: [],
  editorial: [624, 625, 626, 627, 628, 629, 630, 631, 632, 636],
  footnotes: [
    ['unit_037', '(1)', [[611], [612]], 'in_span', { note: 'glosses نفس/كربة' }],
    ['unit_037', '(۱)', [[634], [635, 'رفع', 'pre']], 'in_span', { note: 'takhrij note printed with marker (۱)' }],
  ],
  anchors: [[607, '(1)'], [623, '(١']],
  noise: [
    ['separator', full(613), '":"'],
    ['page_number', full(614), '(۲۸'],
    ['running_header', full(615), null],
    ['separator', full(621), '"."'],
    ['displaced_fragment', full(622), '"لطف" stray fragment'],
    ['separator', full(633), '"(( "'],
    ['displaced_heading', suf('D', 635, 'رفع'), 'tail = heading of unit_039'],
    ['ambiguous_number', full(637), '٣٨ — page-top token, interpreted as unit_038 number'],
    ['separator', full(638), '"-"'],
  ],
  foreign: [{ belongs_to: 'unit_039', from: 635, to: 635, issue_id: 'issue_011', note: 'displaced heading tail' }],
  issues: ['issue_003', 'issue_012'],
  extra: [
    { builder: (u) => u.pushSeg({ role: 'source_attribution', cs: (lineStart(623) + lineText(623).indexOf('[ رواه')), ce: (lineStart(623) + lineText(623).indexOf('(١')) }) },
  ],
});
// ===== UNIT 38 =====
defUnit({
  id: 'unit_038', ordinal: 38, number_raw: '٣٨', l1: 639, l2: 652, base: 0.80,
  title: 'محبة الله تعالى لأوليائه', title_variants: [],
  heading: [639],
  body: [640, 641, 644, 645, 646, 647, 648, 649, 650, 651, IT(652, '[رواه', 'pre')],
  attrib: [[652, '[رواه', 'suf']],
  footnotes: [],
  anchors: [(null)],
  noise: [
    ['separator', full(642), '"-"'],
    ['page_number', full(643), '۲۹'],
  ],
  issues: ['issue_003', 'issue_009'],
});
// ===== UNIT 39 =====
defUnit({
  id: 'unit_039', ordinal: 39, number_raw: '٣٩', l1: 653, l2: 661, base: 0.80,
  title: 'رفع الحرج في الإسلام', title_variants: [],
  heading: [653],
  body: [655, 656, IT(659, '[ حديث حسن', 'pre')],
  attrib: [[659, '[ حديث حسن', 'suf'], [660]],
  footnotes: [['unit_038', '(1)', [[657], [658, 'الأربعين النووية', 'pre']], 'displaced', { host_unit: 'unit_039' }]],
  anchors: [[654, '(1)']],
  noise: [
    ['separator', full(661), 'bullet'],
    ['running_header', suf('R', 658, 'الأربعين النووية'), null],
  ],
  foreign: [
    { belongs_to: 'unit_038', marker: '(', line: 657, issue_id: 'issue_007', note: 'displaced footnote 1 of unit_038' },
  ],
  issues: [],
});
// ===== UNIT 40 =====
defUnit({
  id: 'unit_040', ordinal: 40, number_raw: '٤٠', l1: 662, l2: 671, base: 0.80,
  title: 'كن في الدنيا غريبا', title_variants: [],
  heading: [662],
  body: [663, 664, 665, 666, 667, 668, IT(669, 'رَواهُ', 'pre')],
  attrib: [[669, 'رَواهُ', 'suf']],
  footnotes: [['unit_040', '(٢)', [[678], [679, 'سعة', 'pre']], 'displaced', { host_unit: 'unit_041' }]],
  anchors: [[671, '(۲)']],
  noise: [
    ['displaced_fragment', full(670), '"رَضِي" stray fragment'],
  ],
  issues: ['issue_012'],
});
// ===== UNIT 41 =====
defUnit({
  id: 'unit_041', ordinal: 41, number_raw: '٤١', l1: 672, l2: 683, base: 0.80,
  title: 'اتباع شرع الله على عماد الإيمان', title_variants: ['اتباع شرع / الله / وعل (TOC)'],
  heading: [672],
  body: [675, 676, 680],
  attrib: [681],
  footnotes: [
    ['unit_039', '(1)', [[677]], 'displaced', { host_unit: 'unit_041', note: 'takhrij ابن ماجه الطلاق — belongs to unit_039' }],
    ['unit_040', '(٢)', [[678], [679, 'سعة', 'pre']], 'displaced', { host_unit: 'unit_041' }],
  ],
  anchors: [],
  noise: [
    ['separator', full(673), '"-"'],
    ['garbled_glyph', full(674), '"6" stray digit/glyph'],
    ['separator', full(682), '"-"'],
    ['ambiguous_number', full(683), '٤٢ — page-top token, interpreted as unit_042 number'],
    ['displaced_heading', suf('D', 679, 'سعة'), 'tail = heading of unit_042'],
  ],
  foreign: [
    { belongs_to: 'unit_039', marker: '(', line: 677, issue_id: 'issue_007', note: 'displaced footnote 1 of unit_039' },
    { belongs_to: 'unit_040', marker: '(', line: 678, issue_id: 'issue_007', note: 'displaced footnote 2 of unit_040' },
    { belongs_to: 'unit_042', from: 683, to: 683, issue_id: 'issue_003', note: 'page-top number token interpreted as unit_042 number' },
  ],
  issues: ['issue_017', 'issue_003'],
});
// ===== UNIT 42 =====
defUnit({
  id: 'unit_042', ordinal: 42, number_raw: '٤٢', l1: 684, l2: 700, base: 0.80,
  title: 'سعة مغفرة الله عل', title_variants: ['سعة مغفرة الله على (TOC)'],
  heading: [684],
  body: [686, 687, 688, 689, 690, 691, IT(692, '( رَوَاهُ', 'pre')],
  attrib: [[692, '( رَوَاهُ', 'suf'], [693]],
  footnotes: [
    ['unit_042', '(1)', [[695], [696]], 'in_span', {}],
    ['unit_042', '(٢)', [[697], [698], [699]], 'in_span', {}],
  ],
  anchors: [[685, '(1)'], [694, '(۲)']],
  noise: [
    ['page_number', full(700), '•۳۲'],
  ],
  issues: ['issue_017', 'issue_014'],
});

// ---- IT helper (piece entry) -----------------------------------------------
function IT(l, m, keep) { return { l, m, keep }; }  // unused placeholder
function lineStart(l) { return L(l).start; }
function lineEnd(l) { return L(l).end; }

// =============================================================================
// BUILD UNITS
// =============================================================================
// classify a piece entry's text into isnad / matn
const CHAIN_RE = /عَنْ|عَن|حَدَّثَنَا|حَدَّثنا|أَخْبَرَنَا|أَخْبَرَنا|سَمِعْتُ|حَدِيثُ|بْنِ|بْنُ|بْنِ عُم|أَبي|أبي هُر/;
const MATN_RE = /«|»|\(|\)|قَال رَسُول|قال:[ ]?|يَقُول|قَالَ|فقال|قَال رسول|قال رسول/;
function classifyBodyText(t) {
  return (CHAIN_RE.test(t) && !MATN_RE.test(t)) ? 'isnad' : 'matn';
}
function roleForBodyEntry(e) {
  const text = e._text || '';
  return classifyBodyText(text);
}

function buildUnit(cfg) {
  const u = {
    id: cfg.id,
    type: 'hadith',
    ordinal: cfg.ordinal,
    number_raw: cfg.number_raw,
    title: cfg.title,
    title_variants: cfg.title_variants || [],
    parent_id: 'node_002',
    span: { char_start: L(cfg.l1).start, char_end: L(cfg.l2).end },
    segments: [],
    foreign_spans: [],
    issue_ids: [],
    base_confidence: cfg.base,
  };
  u.raw_text = src.slice(u.span.char_start, u.span.char_end);
  u.pushSeg = (s) => { u.segments.push({ placement: null, ...s }); };
  const base = cfg.base;

  const add = (role, sp) => {
    if (sp && sp.ce > sp.cs) u.pushSeg({ role, cs: sp.cs, ce: sp.ce, confByRole: role });
  };
  const collectPieces = (role, items) => {
    for (const it of items || []) {
      if (typeof it === 'number') add(role, full(it));
      else if (Array.isArray(it)) {
        const [l, m, keep] = it;
        if (m == null) add(role, full(l));
        else { const p = pieces(l, [{ role, m, keep: keep || 'pre' }])[0]; if (p) add(role, p); }
      } else if (it && it.l != null) {
        if (it.m == null) add(role, full(it.l));
        else { const p = pieces(it.l, [{ role, m: it.m, keep: it.keep || 'pre' }])[0]; if (p) add(role, p); }
      } else if (it && it.cs != null && it.ce != null) {
        add(role, { cs: it.cs, ce: it.ce });
      }
    }
  };

  // heading
  collectPieces('heading', cfg.heading || []);

  // body -> isnad / matn
  for (const it of cfg.body || []) {
    let sp = null, text = '';
    if (typeof it === 'number') { sp = full(it); text = lineText(it); }
    else if (Array.isArray(it)) {
      const [l, m, keep] = it;
      if (m == null) { sp = full(l); text = lineText(l); }
      else { const p = pieces(l, [{ role: 'B', m, keep: keep || 'pre' }])[0]; if (p) { sp = { cs: p.cs, ce: p.ce }; text = src.slice(p.cs, p.ce); } }
    } else if (it && it.l != null) {
      if (it.m == null) { sp = full(it.l); text = lineText(it.l); }
      else { const p = pieces(it.l, [{ role: 'B', m: it.m, keep: it.keep || 'pre' }])[0]; if (p) { sp = { cs: p.cs, ce: p.ce }; text = src.slice(p.cs, p.ce); } }
    }
    if (!sp) continue;
    const role = roleForBodyEntry({ _text: text });
    add(role, sp);
  }

  collectPieces('source_attribution', cfg.attrib || []);
  collectPieces('editorial_note', cfg.editorial || []);

  // anchors
  for (const a of cfg.anchors || []) {
    if (a === null) continue;
    const [l, marker] = Array.isArray(a) ? a : [a, null];
    if (marker == null) continue;
    const c = _cut(l, marker);
    u.pushSeg({ role: 'anchor', cs: L(l).start + c.i, ce: L(l).start + c.i + marker.length, confByRole: 'anchor' });
  }

  // extra inline splits
  for (const ex of cfg.extra || []) ex.builder(u);

  // footnotes (owner == this unit)
  for (const f of (cfg.footnotes || [])) {
    const [owner, marker, parts, placement, opts] = f;
    if (owner !== cfg.id) continue;
    defFootnote(cfg.id, marker, parts, placement, opts || {});
  }

  // noise
  for (const n of cfg.noise || []) {
    const [type, sp, note] = n;
    addNoise(type, sp, cfg.id, note || null);
  }

  // foreign (per whole-unit):
  for (const f of cfg.foreign || []) {
    if (f.from != null) {
      const s = spanLines(f.from, f.to);
      foreignIn(cfg.id, f.belongs_to, s, f.issue_id, f.note);
    } else if (f.line != null && f.marker != null) {
      const c = _cut(f.line, f.marker);
      foreignIn(cfg.id, f.belongs_to, { cs: L(f.line).start + c.i, ce: L(f.line).start + c.i + f.marker.length }, f.issue_id, f.note);
    } else if (f.cs != null && f.ce != null) {
      foreignIn(cfg.id, f.belongs_to, { cs: f.cs, ce: f.ce }, f.issue_id, f.note);
    }
  }

  u.issue_ids = (cfg.issues || []).slice();
  units.push(u);
}

for (const cfg of UNITS_CFG) buildUnit(cfg);

// ===== unit_002 special foreign (displaced heading tail of unit_003) =========
{
  const u2 = units.find(x => x.id === 'unit_002');
  const c = _cut(74, 'أركان الإسلام');
  foreignIn('unit_002', 'unit_003', { cs: L(74).start + c.i, ce: L(74).end }, 'issue_011', 'displaced heading of next unit');
  addNoise('displaced_heading', { cs: L(74).start + c.i, ce: L(74).end }, 'unit_002', 'tail = heading of unit_003');
}
// unit_002's neutral 'foreign' config slot was invalid; clear it
for (const cfg of UNITS_CFG) if (cfg.id === 'unit_002') cfg.foreign = [];

// ===== page-top number tokens as noise for ambiguous-number units ============
const AMBIG = [[361, '۲۳'], [377, '٢٤'], [459, '۲۸'], [523, '۳۱'], [570, '۳۵'], [583, '٣٦'], [637, '٣٨'], [683, '٤٢']];
for (const [l, tok] of AMBIG) {
  const host = units.find(x => {
    return x.span.char_start <= L(l).start && L(l).end <= x.span.char_end;
  });
  addNoise('page_number', full(l), host ? host.id : null, 'ambiguous token ' + tok + ' — likely next unit\'s printed number');
}

// ===== footnote foreign spans that were expressed via 'marker' entries ========
// (foreign entries with marker '(' form the span of the marker token only; for
//  multi-line displaced footnotes the host foreign span should match the owner's
//  displaced segment span exactly. Some entries above use marker '(', which gives
//  only the '(1'-prefix — we now correct those to the exact footnote segment span.)

// For each displaced footnote, ensure the host's foreign span == the footnote
// segment span.
{
  const displacedFns = FOOTNOTES.filter(f => f.placement === 'displaced');
  for (const f of displacedFns) {
    f.host_unit = f.host_unit || f.owner;
  }
}

// =============================================================================
// ATTACH FOOTNOTES as segments on owners + build foreign mirrors
// =============================================================================
const foreignIndex = {}; // host -> [{bt,cs,ce}]
for (const ff of foreignList) {
  (foreignIndex[ff.host_unit] = foreignIndex[ff.host_unit] || []).push({ bt: ff.belongs_to, cs: ff.cs, ce: ff.ce, issue_id: ff.issue_id, note: ff.note });
}
// ensure displaced footnotes have mirrors in host units
for (const f of FOOTNOTES.filter(x => x.placement === 'displaced')) {
  const ownerUnit = units.find(x => x.id === f.owner);
  const host = f.host_unit;
  const segSpan = { cs: f.segs[0].cs, ce: f.segs[f.segs.length - 1].ce };
  const lst = foreignIndex[host] = foreignIndex[host] || [];
  if (!lst.some(ent => ent.bt === f.owner && ent.cs === segSpan.cs && ent.ce === segSpan.ce)) {
    lst.push({ bt: f.owner, cs: segSpan.cs, ce: segSpan.ce, issue_id: 'issue_007', note: 'displaced footnote ' + f.id });
  }
}
// dedupe foreign mirrors (union)
for (const hostId of Object.keys(foreignIndex)) {
  const seen = new Set();
  foreignIndex[hostId] = foreignIndex[hostId].filter(e => {
    const k = e.bt + '@' + e.cs + ':' + e.ce;
    if (seen.has(k)) return false;
    seen.add(k); return true;
  });
}

// attach footnote role + marker to owner unit segments
for (const f of FOOTNOTES) {
  const owner = units.find(x => x.id === f.owner);
  if (!owner) throw new Error('owner not found ' + f.owner);
  const text = src.slice(f.segs[0].cs, f.segs[f.segs.length - 1].ce);
  let role = 'footnote';
  if (/أخرجه|رواه|روى|مسلم|البخاري|الترمذي|النسائي|أبو داود|ابن ماجه|الدَّارِمي/.test(text)) role = 'takhrij';
  else if (/قوله|أي |المعنى|يَعني|بمعنى/.test(text)) role = 'gharib';
  const seg = {
    role,
    cs: f.segs[0].cs,
    ce: f.segs[f.segs.length - 1].ce,
    placement: f.placement,
    footnote_marker: f.marker,
    confByRole: role,
  };
  owner.pushSeg(seg);
}

// write foreign spans to units
for (const u of units) {
  u.foreign_spans = (foreignIndex[u.id] || []).map(e => ({
    char_start: e.cs,
    char_end: e.ce,
    belongs_to: e.bt,
    issue_id: e.issue_id,
  }));
}

// ---- noise_ref segments -----------------------------------------------------
for (const u of units) {
  const refs = noiseItems.filter(n => n.unit_id === u.id)
    .map(n => ({ role: 'noise_ref', noise_id: n.id }));
  u.segments.push(...refs.map(r => ({ ...r, isRef: true })));
}

// ---- decorations: placement + confidence + page_index ------------------------
const CONF_ROLE = {
  heading: 0.95, anchor: 0.9, isnad: 0.6, matn: 0.75,
  source_attribution: 0.85, editorial_note: 0.85, footnote: 0.8, takhrij: 0.85, gharib: 0.8,
};
for (const u of units) {
  u.segments = u.segments.filter(s => !s.isRef);
  const noiseRefs = noiseItems.filter(n => n.unit_id === u.id)
    .map(n => ({ role: 'noise_ref', noise_id: n.id }));
  const finalSegs = [];
  for (const s of u.segments) {
    const inside = s.cs >= u.span.char_start && s.ce <= u.span.char_end;
    finalSegs.push({
      role: s.role,
      char_start: s.cs,
      char_end: s.ce,
      page_index: pageIndexFor(s.cs),
      placement: inside ? 'in_span' : 'displaced',
      confidence: (typeof s.confirm !== 'undefined') ? s.confirm : (CONF_ROLE[s.confByRole] || u.base),
    });
  }
  // footnote_marker pass-through
  for (const s of u.segments) {
    if (s.footnote_marker) {
      const tgt = finalSegs.find(t => t.role === s.role && t.char_start === s.cs && t.char_end === s.ce);
      if (tgt) tgt.footnote_marker = s.footnote_marker;
      else finalSegs[finalSegs.findIndex(t => t.char_start === s.cs && t.char_end === s.ce)].footnote_marker = s.footnote_marker;
    }
  }
  finalSegs.push(...noiseRefs);
  u.segments = finalSegs.sort((a, b) => (a.char_start || 0) - (b.char_start || 0) || (a.char_end || 0) - (b.char_end || 0));
  u.span.page_start = pageStartFor(u.span.char_start);
  u.span.page_end = pageEndFor(u.span.char_end);
}

// ---- remove noise_ref placeholders already handled (none) -------------------

// =============================================================================
// CONFIDENCE (Stage 8)
// =============================================================================
const hasDisplaced = u => u.segments.some(s => s.placement === 'displaced');
for (const u of units) {
  let c = u.base;
  // penalties from owned issues
  let pen = 0;
  for (const iid of u.issue_ids) {
    const is = ISSUES.find(x => x.id === iid);
    if (is && is.severity === 'high') pen -= 0.15;
    else if (is && is.severity === 'medium') pen -= 0.05;
  }
  c += pen;
  // displaced / foreign content cap
  if (hasDisplaced(u) || u.foreign_spans.length > 0) c = Math.min(c, 0.8);
  u.confidence = Math.max(0.35, Math.round(c * 100) / 100);
}
// ensure not all equal + <= parent
for (const u of units) if (u.confidence > 0.9) u.confidence = 0.9;

// =============================================================================
// NODES
// =============================================================================
const frontStart = 0;
const frontEnd = L(37).start;
const collStart = L(37).start;
const collEnd = L(701).start;
const indexStart = L(701).start;
const indexEnd = srcLen;

// index noise lines -> carve out of node_003 segments
const INDEX_NOISE_LINES = [710, 711, 712, 733, 779];
const isIndexNoiseLine = l => INDEX_NOISE_LINES.includes(l);

function contiguousRanges(fromLine, toLine, skip) {
  const out = [];
  let cur = [];
  for (let l = fromLine; l <= toLine; l++) {
    if (skip(l)) {
      if (cur.length) { out.push(cur); cur = []; }
      continue;
    }
    cur.push(l);
  }
  if (cur.length) out.push(cur);
  return out.map(group => ({
    role: 'toc_entry',
    char_start: L(group[0]).start,
    char_end: L(group[group.length - 1]).end,
    page_index: pageIndexFor(L(group[0]).start),
    placement: 'in_span',
    confidence: 0.6,
  }));
}
const indexSegments = contiguousRanges(701, 790, isIndexNoiseLine);

const nodes = [
  {
    id: 'node_000', type: 'book', title: 'الأربعين النووية', parent_id: null,
    children: ['node_001', 'node_002', 'node_003'],
    source: { char_start: 0, char_end: srcLen, page_start: 1, page_end: PAGE_OBJS[PAGE_OBJS.length - 1].page_index },
    evidence: 'title page + running headers + TOC + 42 numbered units',
    confidence: 0.95,
  },
  {
    id: 'node_001', type: 'front_matter', title: 'صفحة العنوان وبيانات النشر', parent_id: 'node_000',
    children: [],
    source: { char_start: frontStart, char_end: frontEnd, page_start: 1, page_end: pageEndFor(frontEnd) },
    evidence: 'title page (lines 1-6) + publication data (7-36)',
    confidence: 0.95,
    segments: [
      { role: 'title_page', char_start: L(1).start, char_end: L(7).start, page_index: 1, placement: 'in_span', confidence: 0.95 },
      { role: 'publication_data', char_start: L(7).start, char_end: L(37).start, page_index: 2, placement: 'in_span', confidence: 0.8 },
    ],
  },
  {
    id: 'node_002', type: 'hadith_collection', title: 'متن الأربعين النووية', parent_id: 'node_000',
    children: units.map(u => u.id),
    source: { char_start: collStart, char_end: collEnd, page_start: pageStartFor(collStart), page_end: pageEndFor(collEnd) },
    evidence: '42 numbered hadith units with headings + isnaads',
    confidence: 0.95,
    segments: [],
  },
  {
    id: 'node_003', type: 'index', title: 'الفهرس', parent_id: 'node_000',
    children: [],
    source: { char_start: indexStart, char_end: indexEnd, page_start: pageStartFor(indexStart), page_end: pageEndFor(indexEnd) },
    evidence: 'index/TOC table (lines 701-790)',
    confidence: 0.6,
    segments: indexSegments,
  },
];

// =============================================================================
// ISSUES
// =============================================================================
const occ = (unitId, s, extra) => ({ char_start: s.cs, char_end: s.ce, ...(extra || {}), ...(unitId ? { unit_id: unitId } : {}) });
const occLine = (unitId, l, extra) => ({ char_start: L(l).start, char_end: L(l).end, ...(extra || {}), ...(unitId ? { unit_id: unitId } : {}) });

function addIssue(id, type, severity, description, occurrences, extra) {
  ISSUES.push({ id, type, severity, description, occurrences: occurrences || [], ...(extra || {}) });
}

const segSpanOf = (unitId, role, l) => {
  const u = units.find(x => x.id === unitId);
  const cand = u.segments.find(s => s.role === role && s.char_start === L(l).start);
  return cand ? { cs: cand.char_start, ce: cand.char_end } : full(l);
};
const spanLines_ = (a, b) => spanLines(a, b);

// running headers
const RUNHDR_LINES = [56, 113, 152, 203, 247, 301, 343, 386, 429, 476, 521, 568, 615, 658];
addIssue('issue_001', 'repeated_header', 'low',
  'Running header "الأربعين النووية" repeated at page tops (14 occurrences).',
  RUNHDR_LINES.map(l => occLine(null, l)),
  { confidence: 0.95, needs_visual_check: false });

// page numbers
const PAGENUM_LINES = [110, 213, 225, 232, 279, 291, 306, 341, 365, 385, 406, 409, 451, 547, 594, 643, 700];
const PAGENUM_TAIL = [[246, '.١٤'], [298, '.١٤'], [361, '۲۳'], [377, '٢٤'], [475, '۲۲'], [520, '٢٤'], [567, '.٢٦'], [583, '٣٦'], [614, '(۲۸'], [637, '٣٨'], [683, '٤٢']];
addIssue('issue_002', 'page_number_noise', 'low',
  'Isolated page numbers printed in page footers/headers read as noise.',
  PAGENUM_LINES.map(l => occLine(null, l)).concat(PAGENUM_TAIL.map(([l, m]) => {
    const p = suf('PN', l, m);
    return occ(null, p);
  })),
  { confidence: 0.9, needs_visual_check: false });

// ambiguous page-top number tokens
const AMBIG_OCC = [[361, '۲۳'], [377, '٢٤'], [459, '۲۸'], [523, '۳۱'], [570, '۳۵'], [583, '٣٦'], [637, '٣٨'], [683, '٤٢']];
addIssue('issue_003', 'ambiguous_numbering', 'medium',
  'Page-top tokens (23,24,28,31,35,36,38,42) could be page numbers or the next hadith\'s number; interpreted as the next unit\'s printed number at reduced confidence.',
  AMBIG_OCC.map(([l]) => occLine(null, l)),
  { confidence: 0.7, needs_visual_check: true });

// missing printed numbers
const MISSING_NUM = ['unit_001', 'unit_003', 'unit_004', 'unit_008', 'unit_010', 'unit_011', 'unit_012', 'unit_013', 'unit_014', 'unit_017', 'unit_018', 'unit_019', 'unit_020', 'unit_025'];
addIssue('issue_004', 'missing_number', 'medium',
  'Units for which no printed hadith number was found in the OCR (no inline number and no unambiguous page-top token).',
  MISSING_NUM.map(id => {
    const u = units.find(x => x.id === id);
    return occ(id, { cs: u.span.char_start, ce: u.span.char_end });
  }),
  { confidence: 0.8, needs_visual_check: true });

// possibly missing components
addIssue('issue_005', 'possibly_missing_component', 'medium',
  'For a hadith-collection of this genre, an author introduction (khutbat al-musannif) and an editor introduction are common. ' +
  'Neither is present in the OCR; they may be absent from the edition or lost to OCR.',
  [], { confidence: 0.6, needs_visual_check: true });

// displaced footnotes
const DISPLACED_FNS = [
  ['unit_002', [103, 104]], ['unit_005', [149, 150]], ['unit_005', [152, 152]], ['unit_006', [176, 177]],
  ['unit_009', [201, 201]], ['unit_009', [203, 203]], ['unit_010', [221, 222]], ['unit_011', [243, 244]],
  ['unit_012', [245, 245]], ['unit_014', [265, 267]], ['unit_016', [297, 297]], ['unit_017', [298, 298]],
  ['unit_019', [336, 337]], ['unit_019', [338, 338]], ['unit_023', [383, 384]], ['unit_025', [428, 428]],
  ['unit_026', [447, 447]], ['unit_035', [589, 589]], ['unit_035', [591, 592]], ['unit_038', [657, 658]],
  ['unit_039', [677, 677]], ['unit_040', [678, 679]],
];
addIssue('issue_006', 'displaced_footnote', 'medium',
  'Footnotes whose text was read by OCR into the following unit\'s span (typical page-bottom pattern). Each is reassigned to its logical owner; the host unit\'s foreign_spans records the displaced segment.',
  DISPLACED_FNS.map(([owner, [a, b]]) => {
    const u = units.find(x => x.id === owner);
    const seg = u.segments.find(s => s.role !== 'noise_ref' && s.char_start === L(a).start && s.char_end === L(b).end);
    const sp = seg || spanLines(a, b);
    return occ(owner, sp);
  }),
  { confidence: 0.85, needs_visual_check: false });

// displaced headings (noise tails)
const DISP_HDR = [[74, 'أركان الإسلام', 'unit_003'], [132, 'الورع والإخلاص', 'unit_006'], [178, 'الكسب الحلال سبب إجابة الدعاء', 'unit_010'],
  [222, 'أخوة الإيمان والإسلام', 'unit_013'], [360, 'فضل الله على', 'unit_024'], [405, 'كثرة طرق الخير', 'unit_026'],
  [450, 'الطاعة والتزام السنة', 'unit_028'], [495, 'الوقوف عند حدود الشرع', 'unit_030'],
  [546, 'إزالة المنكر فريضة إسلامية محكمة', 'unit_034'], [592, 'عظيم لطف الله وفضله', 'unit_037'],
  [635, 'رفع الحرج في الإسلام', 'unit_039'], [679, 'سعة مغفرة الله عل', 'unit_042']];
addIssue('issue_007', 'displaced_heading', 'low',
  'Headings read by OCR at the end/beggar of the previous unit\'s span (page-top topic headers repeated before the unit that owns them).',
  DISP_HDR.map(([l, txt, target]) => {
    const noise = noiseItems.find(n => n.type === 'displaced_heading' && n.char_start >= L(l).start && n.char_end <= L(l).end + 0 && Math.abs(n.char_start - L(l).start) < 40);
    const sp = noise ? { cs: noise.char_start, ce: noise.char_end } : full(l);
    return { char_start: sp.cs, char_end: sp.ce, note: 'belongs to ' + target + ' (' + txt + ')' };
  }),
  { confidence: 0.9, needs_visual_check: false });

// displaced fragments / stray noise
const DISP_FRAG = [['unit_004', 108], ['unit_029', 506], ['unit_029', 507], ['unit_037', 622], ['unit_040', 670]];
addIssue('issue_008', 'displaced_fragment', 'medium',
  'Stray fragments read out of order (isnad tail, reordered words).',
  DISP_FRAG.map(([owner, l]) => {
    const n = noiseItems.find(x => x.unit_id === owner && x.char_start === L(l).start && x.type === 'displaced_fragment');
    const sp = n ? { cs: n.char_start, ce: n.char_end } : full(l);
    return occ(owner, sp);
  }),
  { confidence: 0.8, needs_visual_check: true });

// missing footnotes (anchors w/o text)
const MISSING_FN = [['unit_007', 162, '(1)'], ['unit_015', 263, '(۲)'], ['unit_012', 231, '(۲)'], ['unit_040', 671, '(۲)'], ['unit_033', 559, '(1)']];
addIssue('issue_009', 'missing_footnote', 'medium',
  'Anchor markers whose footnote text could not be located (unit_007 (1)@162; unit_015 (1) possibly merged into (٢); unit_012 (٢) tail anchor with no text; unit_040 (2) not found; unit_033 (1)@559).',
  MISSING_FN.map(([owner, l, marker]) => {
    const u = units.find(x => x.id === owner);
    const seg = u.segments.find(s => s.role === 'anchor' && s.char_start >= L(l).start && s.char_end <= L(l).end);
    const sp = seg || full(l);
    return occ(owner, sp);
  }),
  { confidence: 0.6, needs_visual_check: true });

// duplicate fragments
const DUP_FRAG = [[['unit_001', 37, 42], 'heading duplicated on lines 37 and 42'], [['unit_021', 345, 345], '"عَنْ أَبِي عَنْ أَبي عَمْرو" (duplicate كلمة عن أبي)'], [['unit_035', 573, 574], '"أبي هريرة - هريرة"']];
addIssue('issue_010', 'duplicate_fragment', 'low',
  'Same words read twice in a line/adjacent lines.',
  DUP_FRAG.map(([[owner, a, b], note]) => {
    const u = units.find(x => x.id === owner);
    let sp = spanLines(a, b);
    if (a === b) {
      const seg = u.segments.find(s => s.char_start === L(a).start && s.role === 'heading');
      if (seg) sp = { cs: seg.char_start, ce: seg.char_end };
      else {
        const n = noiseItems.find(x => x.unit_id === owner && (x.char_start === L(a).start || x.char_start < L(a).start));
        if (n) sp = { cs: n.char_start, ce: n.char_end };
      }
    }
    return occ(owner, sp, { note });
  }),
  { confidence: 0.9, needs_visual_check: false });

// garbled glyphs
addIssue('issue_011', 'garbled_glyph', 'low',
  'Non-Arabic or decorative glyphs produced by OCR from ornamentation/calligraphy: front title "ท" (line 18), "M" (line 55), "ܙ" (line 340), "6" (line 674), index "។/V/V" (710-712), "Л" (733), "骂骂" (779).',
  [18, 55, 340, 674, 710, 711, 712, 733, 779].map(l => {
    const n = noiseItems.find(x => x.char_start === L(l).start);
    const sp = n ? { cs: n.char_start, ce: n.char_end } : full(l);
    return { char_start: sp.cs, char_end: sp.ce };
  }),
  { confidence: 0.9, needs_visual_check: false });

// ligature losses / truncated headings
const LIG = [['unit_001', 41, 'الله الرحمن الرحيم => بسم الله الرحمن الرحيم (بسم lost)'],
  ['unit_024', 379, '"ن أبي ذر" => "عن أبي ذر" (initial عن lost)'],
  ['unit_025', 408, '"ن أَبِي ذَرِّ" => "عن أبي ذر"'],
  ['unit_042', 684, 'truncated heading "سعة مغفرة الله عل" => "سعة مغفرة الله على"'],
  ['unit_024', 375, 'truncated heading "فضل الله على" => likely "فضل الله على عباده"']];
addIssue('issue_012', 'probable_ligature_loss', 'medium',
  'Honorific/decorative ligature losses and heading truncations where the missing text is strongly indicated by parallels or the TOC.',
  LIG.map(([owner, l, note]) => {
    const u = units.find(x => x.id === owner);
    const seg = u.segments.find(s => s.char_start === L(l).start && (s.role === 'heading' || s.role === 'isnad' || s.role === 'matn'));
    const sp = seg || full(l);
    return occ(owner, sp, { note });
  },
  // using the segment's span keeps issue->segment offsets exactly aligned
  {
    candidates: LIG.map(([, , note]) => ({
      original: note.split('=>')[0].trim(),
      proposed: (note.split('=>')[1] || '').trim() || null,
      reason: 'parallel formulation / TOC entry',
      confidence: 0.7,
    })),
    needs_visual_check: true,
  });

// word-level OCR errors
const OCR_WORDS = [
  ['unit_001', 55, 'الفعلM', 'الفعل [م...]', 'trailing Latin M is a garbled glyph'],
  ['unit_010', 207, 'يَتَأَيُّهَا', 'يَا أَيُّهَا', 'standard formula مكرر في السورة'],
  ['unit_022', 357, 'أَعْلَلْتُ', 'أَحْلَلْتُ', 'parallel wording أَحْلَلْتُ in matn (line 355); editor gloss repeats it'],
  ['unit_024', 380, 'مَا أَنَّهُ', 'مَا أَنَّهُ (؟)', 'breaks flow — possibly "أَنَّهُ" with stray مَا'],
  ['unit_027', 439, 'البرحُسْنُ الخُلُق', 'البِرُّ حُسْنُ الخُلُق', 'word boundary lost'],
  ['unit_029', 490, 'ثلا', 'ثم تلا', 'context: ثم قال/تلا'],
  ['unit_029', 500, 'مَلَاكَ', 'مِلاك/مِلاكَ', 'unknown word; likely مِلاك'],
  ['unit_019', 325, 'الرَّحَاءِ', 'الرَّخَاءِ', 'classical phrase في الرخاء والشدة'],
  ['unit_038', 651, 'يَنطِشُ', 'يَبْطِشُ', 'classical يبطش (grasp)'],
];
addIssue('issue_013', 'probable_ocr_error', 'low',
  'Word-level OCR errors where the source itself shows inconsistency or the reading breaks grammar/idiom.',
  OCR_WORDS.map(([owner, l, orig, prop, why]) => {
    const u = units.find(x => x.id === owner);
    const seg = u.segments.find(s => s.char_start === L(l).start);
    const sp = seg || full(l);
    return occ(owner, sp, { note: orig + ' => ' + prop + ' (' + why + ')' });
  }),
  {
    candidates: OCR_WORDS.map(([, , orig, prop, why]) => ({ original: orig, proposed: prop, reason: why, confidence: 0.6 })),
    needs_visual_check: true,
  });

// ambiguous fragment
addIssue('issue_014', 'ambiguous_fragment', 'medium',
  '"القسامة (٢٥)" line 264 — marginal fragment inserted mid-unit; refers to unit_014 content (القسامة/الديات) but labelled with number ٢٥.',
  [{
    unit_id: 'unit_015',
    char_start: L(264).start,
    char_end: L(264).end,
  }], { confidence: 0.6, needs_visual_check: true });

// heading vs TOC mismatches
addIssue('issue_015', 'heading_toc_mismatch', 'low',
  'Body headings vs TOC entries differ for units 24, 41, 42 (TOC: "فضل الله وعل" / "اتباع شرع…وعل" / "سعة مغفرة الله على"). Body text kept as title; TOC kept in title_variants.',
  [
    { unit_id: 'unit_024', char_start: L(375).start, char_end: L(375).end },
    { unit_id: 'unit_041', char_start: L(672).start, char_end: L(672).end },
    { unit_id: 'unit_042', char_start: L(684).start, char_end: L(684).end },
  ], { confidence: 0.8, needs_visual_check: true });

// unit with multiple reports
addIssue('issue_016', 'unit_with_multiple_reports', 'low',
  'unit_027 contains two reports (النواس بن سمعان and وابصة بن معبد) under one heading/number — kept as a single unit.',
  [{ unit_id: 'unit_027', char_start: L(436).start, char_end: L(460).end }], { confidence: 0.8, needs_visual_check: false });

// unclear footnote attribution for unit_006's own f-lines (markers without
// dedicated anchor) — inventory note
addIssue('issue_017', 'unanchored_footnote', 'low',
  'Footnotes with visible text but no matching in-text anchor marker found in the OCR (unit_006 fn1/fn2 hosted at unit_008; unit_014 fn; unit_025 fn; unit_035 fns).',
  DISPLACED_FNS.filter(([owner]) => ['unit_006', 'unit_014', 'unit_025'].includes(owner)).map(([owner, [a, b]]) => {
    const u = units.find(x => x.id === owner);
    const seg = u.segments.find(s => s.role !== 'noise_ref' && s.char_start === L(a).start && s.char_end === L(b).end);
    const sp = seg || spanLines(a, b);
    return occ(owner, sp);
  }),
  { confidence: 0.7, needs_visual_check: true });

// ---- attach issues to units (link-back) --------------------------------------
const issueByUnit = {};
for (const is of ISSUES) {
  for (const o of is.occurrences || []) {
    if (o && o.unit_id) (issueByUnit[o.unit_id] = issueByUnit[o.unit_id] || new Set()).add(is.id);
  }
}
for (const u of units) {
  if (issueByUnit[u.id]) for (const i of issueByUnit[u.id]) if (!u.issue_ids.includes(i)) u.issue_ids.push(i);
  u.issue_ids = [...new Set(u.issue_ids)];
}

// ---- noise spans final (global list in document order) ------------------------
noiseItems.sort((a, b) => a.char_start - b.char_start || a.char_end - b.char_end);

// ---- profile ------------------------------------------------------------------
const bookProfile = {
  title: 'الأربعين النووية',
  author: 'أبو زكريا يحيى بن شرف النووي',
  editor_commentator: 'أحمد عبد الرازق البكري (تخريج أحاديثه وشرح غريبه)',
  publisher: 'دار السلام',
  edition: 'الطبعة الرابعة',
  publication_year: '١٤٢٨هـ / ٢٠٠٧م',
  isbn: '977-342-075-2',
  genre: 'hadith_collection',
  language: 'ar',
  source_file: path.basename(INPUT),
  source_sha256: crypto.createHash('sha256').update(fs.readFileSync(INPUT)).digest('hex'),
  source_length_chars: srcLen,
  line_endings: src.includes('\r\n') ? 'CRLF' : 'LF',
  sources_used: [
    'the-40-ocr-fastocr.txt',
    'the-40-searchable-fastocr.pdf (page boundaries only; its text layer is RTL-scrambled, so page ranges were aligned by fuzzy n-gram matching)',
  ],
  expected_components: [
    { component: 'author_introduction', status: 'absent_or_undetermined', issue_id: 'issue_005' },
    { component: 'editor_introduction', status: 'absent_or_undetermined', issue_id: 'issue_005' },
    { component: 'toc', status: 'present' },
    { component: 'index', status: 'present' },
    { component: 'publisher_title_page', status: 'present' },
  ],
  segment_roles_used: ['heading', 'isnad', 'matn', 'source_attribution', 'anchor', 'takhrij', 'gharib', 'footnote', 'editorial_note', 'noise_ref'],
  structure_summary: 'flat collection: front matter (title page + publication data, lines 1-36) → 42 numbered hadith units (lines 37-700) → back-matter index (الفهرس, lines 701-790). No chapters or sections. Each unit: heading + body (isnad+matn) + attribution (رواه…) + editor footnotes (takhrij/gharib). Page model derived from the searchable PDF by fuzzy text alignment (32 pages; printed page numbers not present in the PDF text layer — null). OCR noise: running headers, page numbers, displaced headings/footnotes, garbled glyphs.',
  notes: [
    'Page model built from the searchable PDF text layer by stripped n-gram alignment (extract_pages.py); printed_page_number null because the searchable layer does not contain isolated printed numbers. Pages 3-32 cover the hadith collection and index; pages 1-2 cover title/publication data.',
    'Numbering systems mixed in source: Arabic-Indic (٢٤٥), Persian (۲۴۵), ASCII (34). Treated as equivalent; original digits preserved in number_raw.',
    'Footnotes are frequently read by the OCR after the next unit\'s heading; each is reassigned to its logical owner and cross-referenced via foreign_spans (issue_006).',
    'isnad/matn boundaries inside scrambled OCR lines are approximate (isnad segments at lower confidence); the role assignment is best-effort per-line classification.',
  ],
};

// =============================================================================
// SANITY CHECKS (fast in-script validation before the external validator)
// =============================================================================
let errors = 0;
function err(msg) { errors++; console.log('  FAIL: ' + msg); }
function coverage(intervals, from, to) {
  let iv = intervals.map(p => ({ cs: Math.max(p.cs, from), ce: Math.min(p.ce, to) })).filter(p => p.ce > p.cs).sort(spanSort);
  let cur = from, gaps = [];
  for (const p of iv) { if (p.cs > cur) gaps.push([cur, p.cs]); if (p.ce > cur) cur = p.ce; }
  return { full: cur === to, end: cur, gaps };
}
for (const u of units) {
  const from = u.span.char_start, to = u.span.char_end;
  const segs = u.segments.filter(s => s.role !== 'noise_ref');
  const realGaps = coverage(segs.concat(u.foreign_spans.map(f => ({ cs: f.char_start, ce: f.char_end }))), from, to)
    .gaps.filter(g => !/^[\s]+$/.test(src.slice(g[0], g[1])));
  if (realGaps.length) err(u.id + ' non-ws coverage gap ' + JSON.stringify(realGaps.slice(0, 3)));
}
// unit span tiling
{
  const sorted = units.slice().sort((a, b) => a.span.char_start - b.span.char_start);
  let prev = null;
  for (const uY of sorted) {
    if (prev && uY.span.char_start < prev) err('overlap');
    prev = uY.span.char_end;
  }
}
// segments in range
for (const u of units) {
  for (const s of u.segments) {
    if (s.role === 'noise_ref') continue;
    if (s.char_start < 0 || s.char_end > srcLen || s.char_start >= s.char_end) err(u.id + ' bad segment span');
  }
}
// JSON fields
const dropFields = u => {
  const copy = JSON.parse(JSON.stringify(u, (k, v) => (k === 'isRef' || k === 'confByRole' || k === 'base_confidence') ? undefined : v));
  delete copy.base_confidence;
  return copy;
};
const unitOut = units.map(dropFields);

// noise_items referenced by noise_ref must exist
for (const n of noiseItems) if (!n.id) err('noise without id');
const noiseIds = new Set(noiseItems.map(n => n.id));
for (const u of units) for (const s of u.segments) {
  if (s.role === 'noise_ref' && !noiseIds.has(s.noise_id)) err(u.id + ' noise_ref missing ' + s.noise_id);
}

// ---- write outputs ------------------------------------------------------------
fs.mkdirSync(OUTDIR, { recursive: true });
function write(name, obj) {
  fs.writeFileSync(path.join(OUTDIR, name), JSON.stringify(obj, null, 2) + '\n', 'utf8');
}
write('book_profile.json', bookProfile);
write('structure.json', {
  root: 'node_000',
  pages: PAGE_OBJS,
  noise_spans: noiseItems.map(n => ({ id: n.id, type: n.type, char_start: n.char_start, char_end: n.char_end, ...(n.note ? { note: n.note } : {}) })),
  nodes: nodes,
});
write('units.json', { units: unitOut });
write('issues.json', { issues: ISSUES });

console.log('=== VALIDATION ===');
console.log('units: ' + units.length + '  footnotes: ' + FOOTNOTES.length + '  noise: ' + noiseItems.length + '  issues: ' + ISSUES.length + '  pages: ' + PAGE_OBJS.length);
console.log(errors === 0 ? 'ALL IN-SCRIPT CHECKS PASSED' : errors + ' ERROR(S)');
console.log('output written to ' + OUTDIR);