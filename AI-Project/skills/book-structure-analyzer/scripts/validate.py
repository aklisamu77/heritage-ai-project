#!/usr/bin/env python3
"""
validate.py — mandatory validator for book-structure-analyzer output.

Usage:
    python validate.py --source path/to/book-ocr.txt --out path/to/output_dir

Reads book_profile.json, structure.json, units.json, issues.json from --out,
checks them against the source text, and writes validation_report.json
into the same directory.

Exit code 0 = no errors (warnings allowed); 1 = errors found; 2 = could not run.

Standard library only. Offsets are Unicode code-point indices into the source
decoded as UTF-8 with line endings exactly as on disk; char_end is exclusive.
"""

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict

REQUIRED_FILES = ["book_profile.json", "structure.json", "units.json", "issues.json"]
FORBIDDEN_KEYS = {"cs", "ce", "start", "end"}  # must use char_start / char_end
LONG_NOISE_WARN = 60      # noise spans longer than this are suspicious
MAX_LISTED = 40           # cap on examples listed per finding


class Report:
    def __init__(self):
        self.errors = defaultdict(list)
        self.warnings = defaultdict(list)
        self.stats = {}

    def err(self, check, msg):
        self.errors[check].append(msg)

    def warn(self, check, msg):
        self.warnings[check].append(msg)

    def as_dict(self):
        def pack(d):
            return {k: {"count": len(v), "examples": v[:MAX_LISTED]} for k, v in sorted(d.items())}
        return {
            "status": "fail" if self.errors else "pass",
            "error_count": sum(len(v) for v in self.errors.values()),
            "warning_count": sum(len(v) for v in self.warnings.values()),
            "errors": pack(self.errors),
            "warnings": pack(self.warnings),
            "stats": self.stats,
        }


# ---------------------------------------------------------------- loading

def load_json(path, rep):
    raw = open(path, "rb").read()
    name = os.path.basename(path)
    if raw.startswith(b"\xef\xbb\xbf"):
        rep.err("encoding", f"{name}: starts with a UTF-8 BOM (must be UTF-8 without BOM)")
        raw = raw[3:]
    if b"\r\n" in raw:
        rep.warn("encoding", f"{name}: uses CRLF line endings (LF expected)")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        rep.err("encoding", f"{name}: not valid UTF-8 ({e})")
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        rep.err("json", f"{name}: invalid JSON ({e})")
        return None


def find_forbidden_keys(obj, path, rep, fname):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in FORBIDDEN_KEYS:
                rep.err("field_names", f"{fname}:{path}.{k} — use char_start/char_end")
            find_forbidden_keys(v, f"{path}.{k}", rep, fname)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            find_forbidden_keys(v, f"{path}[{i}]", rep, fname)


# ---------------------------------------------------------------- helpers

def span_of(d):
    """Return (start, end) from a dict with char_start/char_end, or None."""
    if not isinstance(d, dict):
        return None
    s, e = d.get("char_start"), d.get("char_end")
    if isinstance(s, int) and isinstance(e, int):
        return s, e
    return None


def check_range(label, s, e, n, rep):
    if s < 0 or e > n or s >= e:
        rep.err("offset_range", f"{label}: invalid range [{s},{e}) for source length {n}")
        return False
    return True


def snippet(src, s, e, k=50):
    t = src[s:e].replace("\n", "⏎")
    return t if len(t) <= k else t[:k] + "…"


# ---------------------------------------------------------------- main checks

def validate(source_path, out_dir):
    rep = Report()

    # --- source
    src_bytes = open(source_path, "rb").read()
    if src_bytes.startswith(b"\xef\xbb\xbf"):
        rep.warn("source", "source file starts with a BOM; offsets must count the BOM character (U+FEFF) as index 0")
    try:
        src = src_bytes.decode("utf-8")
    except UnicodeDecodeError as e:
        rep.err("source", f"source is not valid UTF-8: {e}")
        return rep
    N = len(src)

    # --- output files
    data = {}
    for f in REQUIRED_FILES:
        p = os.path.join(out_dir, f)
        if not os.path.exists(p):
            rep.err("files", f"missing {f}")
            continue
        data[f] = load_json(p, rep)
    if any(data.get(f) is None for f in REQUIRED_FILES):
        return rep
    for f, obj in data.items():
        find_forbidden_keys(obj, "$", rep, f)

    profile = data["book_profile.json"]
    structure = data["structure.json"]
    units = data["units.json"].get("units", [])
    issues = data["issues.json"].get("issues", [])
    nodes = structure.get("nodes", [])
    noise = structure.get("noise_spans", [])
    pages = structure.get("pages", [])

    # --- profile vs source
    sha = hashlib.sha256(src_bytes).hexdigest()
    if profile.get("source_sha256") != sha:
        rep.err("profile", f"source_sha256 mismatch (actual {sha})")
    if profile.get("source_length_chars") != N:
        rep.err("profile", f"source_length_chars={profile.get('source_length_chars')} but actual length is {N}")
    le = "CRLF" if "\r\n" in src else "LF"
    if profile.get("line_endings") != le:
        rep.err("profile", f"line_endings={profile.get('line_endings')} but source uses {le}")

    # --- ids
    node_ids = [n.get("id") for n in nodes]
    unit_ids = [u.get("id") for u in units]
    issue_ids = [i.get("id") for i in issues]
    noise_ids = [z.get("id") for z in noise]
    for kind, ids in (("node", node_ids), ("unit", unit_ids), ("issue", issue_ids), ("noise", noise_ids)):
        for i, c in Counter(ids).items():
            if c > 1:
                rep.err("ids", f"duplicate {kind} id {i} ({c}x)")
            if not i:
                rep.err("ids", f"{kind} without id")
    node_by = {n["id"]: n for n in nodes if n.get("id")}
    unit_by = {u["id"]: u for u in units if u.get("id")}
    issue_by = {i["id"]: i for i in issues if i.get("id")}
    all_owner_ids = set(node_by) | set(unit_by)

    # --- tree
    root = structure.get("root")
    if root not in node_by:
        rep.err("tree", f"root {root} not found among nodes")
    for n in nodes:
        pid = n.get("parent_id")
        if pid is not None and pid not in node_by:
            rep.err("tree", f"{n['id']}: parent_id {pid} does not exist")
        for c in n.get("children", []):
            if c not in node_by and c not in unit_by:
                rep.err("tree", f"{n['id']}: child {c} does not exist")
            elif c in node_by and node_by[c].get("parent_id") != n["id"]:
                rep.err("tree", f"{n['id']} lists child {c} but {c}.parent_id={node_by[c].get('parent_id')}")
        sp = span_of(n.get("source"))
        if sp:
            check_range(f"node {n['id']}.source", *sp, N, rep)
        else:
            rep.err("schema", f"node {n['id']}: source.char_start/char_end missing")

    # --- pages
    if pages:
        prev_end = None
        for p in pages:
            sp = span_of(p)
            if not sp:
                rep.err("pages", f"page {p.get('page_index')}: missing offsets")
                continue
            check_range(f"page {p.get('page_index')}", *sp, N, rep)
            if prev_end is not None and sp[0] < prev_end:
                rep.err("pages", f"page {p.get('page_index')} overlaps previous page")
            prev_end = sp[1]
        page_index_set = {p.get("page_index") for p in pages}
    else:
        page_index_set = None
        rep.warn("pages", "no page model; footnote-to-page attribution cannot be verified")

    # --- coverage map: char -> list of owners
    cover = defaultdict(list)

    def claim(s, e, owner):
        for i in range(s, e):
            cover[i].append(owner)

    # noise
    for z in noise:
        sp = span_of(z)
        if not sp:
            rep.err("schema", f"noise {z.get('id')}: missing char_start/char_end")
            continue
        if not check_range(f"noise {z['id']}", *sp, N, rep):
            continue
        if "text" in z and z["text"] != src[sp[0]:sp[1]]:
            rep.err("offset_text", f"noise {z['id']}: stored text != source slice")
        claim(*sp, f"noise:{z['id']}")
        if sp[1] - sp[0] > LONG_NOISE_WARN and z.get("source") != "override":
            rep.warn("noise_too_long",
                     f"noise {z['id']} ({z.get('type')}) is {sp[1]-sp[0]} chars — noise must cover only the noise characters: «{snippet(src,*sp)}»")

    # node-level segments (front/back matter)
    for n in nodes:
        for k, sg in enumerate(n.get("segments", [])):
            sp = span_of(sg)
            if sp and check_range(f"node {n['id']}.segments[{k}]", *sp, N, rep):
                claim(*sp, f"node:{n['id']}:{sg.get('role')}")

    # --- units
    segments_all = []  # (unit_id, seg)
    for u in units:
        uid = u.get("id")
        for key in ("type", "ordinal", "title", "parent_id", "span", "raw_text", "segments",
                    "foreign_spans", "issue_ids", "confidence"):
            if key not in u:
                rep.err("schema", f"{uid}: missing field '{key}'")
        if u.get("parent_id") not in node_by:
            rep.err("tree", f"{uid}: parent_id {u.get('parent_id')} does not exist")
        sp = span_of(u.get("span"))
        if not sp:
            rep.err("schema", f"{uid}: span.char_start/char_end missing")
            continue
        if not check_range(f"{uid}.span", *sp, N, rep):
            continue
        if u.get("raw_text") != src[sp[0]:sp[1]]:
            rep.err("offset_text", f"{uid}: raw_text != source[span] — offsets were not computed from the source")

        seen = set()
        for k, sg in enumerate(u.get("segments", [])):
            if sg.get("role") == "noise_ref":
                if sg.get("noise_id") not in set(noise_ids):
                    rep.err("refs", f"{uid}: noise_ref to unknown {sg.get('noise_id')}")
                continue
            ssp = span_of(sg)
            if not ssp:
                rep.err("schema", f"{uid}.segments[{k}]: missing char_start/char_end")
                continue
            if not check_range(f"{uid}.segments[{k}]", *ssp, N, rep):
                continue
            key = (sg.get("role"), ssp)
            if key in seen:
                rep.err("duplicate_segment", f"{uid}: segment {sg.get('role')} [{ssp[0]},{ssp[1]}) listed twice")
                continue
            seen.add(key)
            if "text" in sg and sg["text"] != src[ssp[0]:ssp[1]]:
                rep.err("offset_text", f"{uid}.segments[{k}]: stored text != source slice")
            inside = sp[0] <= ssp[0] and ssp[1] <= sp[1]
            pl = sg.get("placement")
            if inside and pl != "in_span":
                rep.err("placement", f"{uid}: segment [{ssp[0]},{ssp[1]}) is inside the span but placement={pl}")
            if not inside and pl != "displaced":
                rep.err("placement", f"{uid}: segment [{ssp[0]},{ssp[1]}) is outside the span but placement={pl}")
            if page_index_set is not None and sg.get("page_index") is not None and sg["page_index"] not in page_index_set:
                rep.err("pages", f"{uid}: segment page_index {sg['page_index']} not in page model")
            claim(*ssp, f"unit:{uid}:{sg.get('role')}")
            segments_all.append((uid, sg, ssp, inside))

        # footnotes need a marker
        for sg in u.get("segments", []):
            if sg.get("role") in ("footnote", "takhrij", "gharib") and span_of(sg):
                if not sg.get("footnote_marker") and not sg.get("issue_ids"):
                    rep.warn("footnote_marker",
                             f"{uid}: {sg['role']} [{sg['char_start']},{sg['char_end']}) has no footnote_marker and no issue")

        for c in u.get("issue_ids", []):
            if c not in issue_by:
                rep.err("refs", f"{uid}: issue_id {c} does not exist")

        # roles
        roles = Counter(sg.get("role") for sg in u.get("segments", []))
        if "heading" not in roles and u.get("title"):
            rep.warn("roles", f"{uid}: has a title but no heading segment")

    # --- displaced segments must be mirrored by foreign_spans of the host unit
    unit_spans = [(u["id"], span_of(u.get("span"))) for u in units if span_of(u.get("span"))]

    def host_of(s, e):
        return [uid for uid, sp in unit_spans if sp[0] <= s and e <= sp[1]]

    foreign_index = defaultdict(list)  # host uid -> list of (s,e,belongs_to)
    for u in units:
        uid = u["id"]
        fs_list = u.get("foreign_spans", [])
        seen = set()
        for k, fs in enumerate(fs_list):
            sp = span_of(fs)
            if not sp:
                rep.err("schema", f"{uid}.foreign_spans[{k}]: missing offsets")
                continue
            if sp in seen:
                rep.err("duplicate_segment", f"{uid}: foreign span [{sp[0]},{sp[1]}) listed twice")
            seen.add(sp)
            if fs.get("belongs_to") not in unit_by:
                rep.err("refs", f"{uid}.foreign_spans[{k}]: belongs_to {fs.get('belongs_to')} is not a unit "
                                f"(noise inside a span is a noise span + noise_ref, not a foreign span)")
            foreign_index[uid].append((sp[0], sp[1], fs.get("belongs_to")))
        # overlap between foreign spans of the same unit
        fl = sorted(foreign_index[uid])
        for a, b in zip(fl, fl[1:]):
            if b[0] < a[1]:
                rep.err("overlap", f"{uid}: foreign spans [{a[0]},{a[1]}) and [{b[0]},{b[1]}) overlap")

    for uid, sg, ssp, inside in segments_all:
        if inside:
            continue
        hosts = host_of(*ssp)
        for h in hosts:
            if not any(s == ssp[0] and e == ssp[1] and bt == uid for s, e, bt in foreign_index[h]):
                rep.err("foreign_mirror",
                        f"{uid}: displaced segment [{ssp[0]},{ssp[1]}) lies in {h} but {h}.foreign_spans has no exact entry belonging_to {uid}")
    for h, lst in foreign_index.items():
        for s, e, bt in lst:
            if bt in unit_by and not any(uid == bt and ssp == (s, e) for uid, _, ssp, _ in segments_all):
                rep.err("foreign_mirror",
                        f"{h}: foreign span [{s},{e}) says it belongs to {bt}, but {bt} has no segment with these exact offsets")

    # --- unit span overlap
    us = sorted((sp[0], sp[1], uid) for uid, sp in unit_spans)
    for a, b in zip(us, us[1:]):
        if b[0] < a[1]:
            rep.err("overlap", f"unit spans {a[2]} and {b[2]} overlap")

    # --- coverage: every non-whitespace char covered exactly once
    uncovered, multi = [], []
    for i, ch in enumerate(src):
        if ch.isspace():
            continue
        owners = cover.get(i, [])
        if not owners:
            uncovered.append(i)
        elif len(set(owners)) > 1:
            multi.append(i)

    def ranges(idx):
        out = []
        for i in idx:
            if out and i <= out[-1][1] + 1:
                out[-1][1] = i
            else:
                out.append([i, i])
        return [(a, b + 1) for a, b in out]

    for s, e in ranges(uncovered):
        rep.err("coverage_gap", f"[{s},{e}) not covered by any segment/noise: «{snippet(src,s,e)}»")
    for s, e in ranges(multi):
        owners = sorted(set(cover[s]))
        rep.err("coverage_overlap", f"[{s},{e}) claimed by {owners}: «{snippet(src,s,e)}»")

    # --- numbering
    by_group = defaultdict(list)
    for u in units:
        by_group[(u.get("parent_id"), u.get("type"))].append(u)
    for (pid, typ), grp in by_group.items():
        ords = [u.get("ordinal") for u in grp]
        if any(o is None for o in ords):
            rep.err("numbering", f"{typ} under {pid}: {sum(o is None for o in ords)} units have ordinal=null "
                                 f"(ordinal is the logical position and must always be set)")
        clean = [o for o in ords if isinstance(o, int)]
        if clean:
            if len(clean) != len(set(clean)):
                rep.err("numbering", f"{typ} under {pid}: duplicate ordinals")
            exp = list(range(min(clean), min(clean) + len(clean)))
            if sorted(clean) != exp:
                rep.err("numbering", f"{typ} under {pid}: ordinals are not consecutive")
            order = [u.get("ordinal") for u in sorted(grp, key=lambda x: span_of(x.get("span")) or (0, 0))]
            if None not in order and order != sorted(order):
                rep.warn("numbering", f"{typ} under {pid}: ordinal order differs from text order")

    # --- confidence
    confs = []
    for u in units:
        c = u.get("confidence")
        if not isinstance(c, (int, float)) or not 0 <= c <= 1:
            rep.err("confidence", f"{u['id']}: confidence {c} not in [0,1]")
            continue
        confs.append(c)
        pc = node_by.get(u.get("parent_id"), {}).get("confidence")
        if isinstance(pc, (int, float)) and c > pc + 1e-9:
            rep.err("confidence", f"{u['id']}: confidence {c} > parent {u.get('parent_id')} confidence {pc}")
        has_displaced = any(sg.get("placement") == "displaced" for sg in u.get("segments", []))
        if (has_displaced or u.get("foreign_spans")) and c > 0.8:
            rep.err("confidence", f"{u['id']}: has displaced/foreign content but confidence {c} > 0.8")
        sev = [issue_by[i].get("severity") for i in u.get("issue_ids", []) if i in issue_by]
        if "high" in sev and c > 0.8:
            rep.warn("confidence", f"{u['id']}: linked to a high-severity issue but confidence {c}")
    if len(confs) > 3 and len(set(confs)) == 1:
        rep.warn("confidence", f"all {len(confs)} units share confidence {confs[0]} — re-check calibration")

    # --- issues
    for i in issues:
        iid = i.get("id")
        for key in ("type", "severity", "description", "confidence"):
            if key not in i:
                rep.err("schema", f"{iid}: missing '{key}'")
        c = i.get("confidence")
        if not isinstance(c, (int, float)) or not 0 <= c <= 1:
            rep.err("confidence", f"{iid}: confidence {c} not in [0,1]")
        if i.get("severity") not in ("low", "medium", "high"):
            rep.err("schema", f"{iid}: severity {i.get('severity')} invalid")
        locs = list(i.get("occurrences") or [])
        if i.get("location"):
            locs.append(i["location"])
        if not locs and i.get("type") not in ("page_boundary_unknown", "possibly_missing_component"):
            rep.warn("issue_location", f"{iid}: no location/occurrences")
        for k, loc in enumerate(locs):
            sp = span_of(loc)
            if sp:
                check_range(f"{iid}.occurrence[{k}]", *sp, N, rep)
                if "text" in loc and loc["text"] != src[sp[0]:sp[1]]:
                    rep.err("offset_text", f"{iid}.occurrence[{k}]: text != source slice")
            if loc.get("unit_id") and loc["unit_id"] not in unit_by:
                rep.err("refs", f"{iid}.occurrence[{k}]: unit_id {loc['unit_id']} does not exist")
            if sp and loc.get("unit_id") in unit_by:
                u = unit_by[loc["unit_id"]]
                known = {span_of(u.get("span"))} | {span_of(s) for s in u.get("segments", [])} \
                        | {span_of(f) for f in u.get("foreign_spans", [])}
                usp = span_of(u.get("span"))
                touches = usp and not (sp[1] <= usp[0] or sp[0] >= usp[1])
                seg_touch = any(s and not (sp[1] <= s[0] or sp[0] >= s[1]) for s in known)
                if not touches and not seg_touch:
                    rep.err("issue_location", f"{iid}.occurrence[{k}] [{sp[0]},{sp[1]}) does not touch {loc['unit_id']}")
        # the same object referenced by an issue and a segment should have identical offsets
        for k, loc in enumerate(locs):
            sp = span_of(loc)
            if not sp or loc.get("unit_id") not in unit_by:
                continue
            u = unit_by[loc["unit_id"]]
            cand = [span_of(s) for s in u.get("segments", []) if span_of(s)] + \
                   [span_of(f) for f in u.get("foreign_spans", []) if span_of(f)]
            if sp == span_of(u.get("span")):
                continue  # occurrence refers to the whole unit
            same_start = [c for c in cand if c[0] == sp[0]]
            if same_start and sp not in same_start:
                rep.err("offset_consistency",
                        f"{iid}.occurrence[{k}] [{sp[0]},{sp[1]}) starts where a segment starts but ends differently {same_start}")

    # issues referenced by units should link back (warning only)
    linked = {x for u in units for x in u.get("issue_ids", [])}
    for i in issues:
        occ_units = {l.get("unit_id") for l in (i.get("occurrences") or []) if l.get("unit_id")}
        if occ_units and i["id"] not in linked:
            rep.warn("refs", f"{i['id']} has occurrences in units but no unit lists it in issue_ids")

    # --- footnote <-> anchor pairing (engine output carries footnote_id on both)
    issue_occ = defaultdict(list)  # type -> list of (unit_id, s, e)
    for i in issues:
        for o in (i.get("occurrences") or []) + ([i["location"]] if i.get("location") else []):
            sp = span_of(o)
            if sp:
                issue_occ[i.get("type")].append((o.get("unit_id"), sp[0], sp[1]))

    def flagged(typ, uid, sp):
        return any(u == uid and not (sp[1] <= s or sp[0] >= e) for u, s, e in issue_occ.get(typ, []))

    fn_roles = {"takhrij", "gharib", "footnote"}
    any_fid = False
    for u in units:
        uid = u["id"]
        fsegs = [s for s in u.get("segments", []) if s.get("role") in fn_roles and span_of(s)]
        anchors = [s for s in u.get("segments", []) if s.get("role") == "anchor" and span_of(s)]
        if fsegs and not any("footnote_id" in s for s in fsegs):
            rep.warn("footnote_ids", f"{uid}: footnote segments carry no footnote_id; anchor pairing cannot be checked")
            continue
        any_fid = any_fid or bool(fsegs)
        a_fids = Counter(a.get("footnote_id") for a in anchors if a.get("footnote_id"))
        for f, c in a_fids.items():
            if c > 1:
                rep.err("footnote_pairing", f"{uid}: {c} anchors point to the same footnote {f}")
        seen_f = set()
        for s in fsegs:
            f = s.get("footnote_id")
            if f in seen_f:
                continue
            seen_f.add(f)
            if f not in a_fids and not flagged("unanchored_footnote", uid, span_of(s)):
                rep.err("footnote_pairing", f"{uid}: footnote {f} ({s.get('footnote_marker')}) has no anchor in its unit "
                                            f"and no unanchored_footnote issue")
        for a in anchors:
            if not a.get("footnote_id") and not flagged("orphan_anchor", uid, span_of(a)):
                rep.warn("footnote_pairing", f"{uid}: anchor {a.get('marker')} [{a['char_start']},{a['char_end']}) has no footnote and no orphan_anchor issue")

    # --- noise must not look like main text
    for z in noise:
        sp = span_of(z)
        if not sp or z.get("type") in ("displaced_heading", "running_header") or z.get("source") == "override":
            continue
        t = src[sp[0]:sp[1]]
        if any(k in t for k in ("«", "»", "رواه", "رَوَاهُ", "ﷺ")) or len(re.findall(r"[\u0621-\u064A]{2,}", t)) >= 4:
            rep.warn("noise_content", f"noise {z['id']} ({z.get('type')}) looks like real text: «{snippet(src,*sp)}»")

    # --- fragmentation: same role split into pieces separated only by whitespace
    for u in units:
        segs = sorted([s for s in u.get("segments", []) if span_of(s)], key=lambda s: s["char_start"])
        for a, b in zip(segs, segs[1:]):
            if a.get("role") == b.get("role") and a.get("footnote_id") == b.get("footnote_id") \
                    and a.get("placement") == b.get("placement") and src[a["char_end"]:b["char_start"]].strip() == "":
                rep.warn("fragmented_segments", f"{u['id']}: two '{a['role']}' segments separated only by whitespace "
                                                f"at [{a['char_end']},{b['char_start']}) — merge them")

    # --- page confidence honesty: a confident page boundary should sit at a header / after a page number
    if pages and noise:
        marks = []
        for z in noise:
            sp = span_of(z)
            if not sp:
                continue
            if z.get("type") == "running_header":
                marks.append(sp[0])
            elif z.get("type") == "page_number":
                marks.append(sp[1])
        for p in pages[1:]:
            sp = span_of(p)
            if sp and (p.get("confidence") or 0) >= 0.8 and marks:
                d = min(abs(m - sp[0]) for m in marks)
                if d > 80:
                    rep.warn("page_confidence", f"page {p.get('page_index')} has confidence {p.get('confidence')} but its start "
                                                f"is {d} chars from any running header / page number")

    # --- stats
    role_counts = Counter(sg.get("role") for u in units for sg in u.get("segments", []))
    rep.stats = {
        "source_length_chars": N,
        "nodes": len(nodes),
        "units": len(units),
        "issues": len(issues),
        "noise_spans": len(noise),
        "pages": len(pages),
        "segment_roles": dict(role_counts),
        "body_role_share": round(role_counts.get("body", 0) / max(1, sum(role_counts.values())), 3),
        "covered_non_whitespace_chars": sum(1 for i, ch in enumerate(src) if not ch.isspace() and cover.get(i)),
        "non_whitespace_chars": sum(1 for ch in src if not ch.isspace()),
    }
    if rep.stats["body_role_share"] > 0.5:
        rep.warn("roles", f"{rep.stats['body_role_share']:.0%} of segments use the generic 'body' role — "
                          f"try to use the book's specific roles (low confidence is acceptable)")
    return rep


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True, help="OCR text file")
    ap.add_argument("--out", required=True, help="directory containing the 4 JSON outputs")
    args = ap.parse_args()
    try:
        rep = validate(args.source, args.out)
    except Exception as e:  # pragma: no cover
        print(f"validator crashed: {e!r}", file=sys.stderr)
        sys.exit(2)
    result = rep.as_dict()
    path = os.path.join(args.out, "validation_report.json")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"status: {result['status']}  errors: {result['error_count']}  warnings: {result['warning_count']}")
    for k, v in result["errors"].items():
        print(f"  ERROR {k}: {v['count']}")
    for k, v in result["warnings"].items():
        print(f"  warn  {k}: {v['count']}")
    print(f"report: {path}")
    sys.exit(1 if rep.errors else 0)


if __name__ == "__main__":
    main()
