#!/usr/bin/env python3
"""
run_regression.py — regression test for the book-structure-analyzer scripts.

Rebuilds every approved book case with the current engine.py and compares the output with the
approved snapshot in tests/expected/<case>/. Run it after ANY change to engine.py or
extract_pages.py: a change that fixes one book must not silently break another.

  python tests/run_regression.py                 run all cases
  python tests/run_regression.py --case NAME     run one case
  python tests/run_regression.py --approve NAME  replace NAME's snapshot with the current output
                                                 (only after the user has reviewed and accepted
                                                 every reported difference)

A case passes when the build validates (status pass) and units.json, structure.json and
issues.json equal the snapshot exactly. On a difference the report shows what changed, in book
terms: which characters changed owner or role (with the text), which issues appeared or
disappeared, which page boundaries moved.

When a case names the searchable PDF and it exists, extract_pages.py is also re-run and its
pages.json compared with the stored one (skipped, not failed, when the PDF is absent).

Cases are listed in tests/cases.json. Standard library only (extract_pages.py needs PyMuPDF).
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
SCRIPTS = os.path.join(SKILL, "scripts")
COMPARED = ["units.json", "structure.json", "issues.json"]
MAX_SHOWN = 25


def p(*parts):
    return os.path.normpath(os.path.join(HERE, *parts))


def run(cmd):
    r = subprocess.run([sys.executable] + cmd, capture_output=True, text=True, encoding="utf-8",
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- per-character view

def char_labels(out_dir):
    """char offset -> label: ('unit', ordinal, role[, footnote]) / ('node', id, role) / ('noise', type)"""
    lab = {}
    st = load(os.path.join(out_dir, "structure.json"))
    for n in st["nodes"]:
        for g in n.get("segments", []):
            for i in range(g["char_start"], g["char_end"]):
                lab[i] = ("node", n["id"], g["role"])
    for z in st["noise_spans"]:
        for i in range(z["char_start"], z["char_end"]):
            lab[i] = ("noise", z["type"])
    for u in load(os.path.join(out_dir, "units.json"))["units"]:
        for g in u["segments"]:
            if "char_start" in g:
                for i in range(g["char_start"], g["char_end"]):
                    lab[i] = ("unit", u["ordinal"], g["role"], g.get("footnote_id", ""))
    return lab


def show(lab):
    if lab is None:
        return "(nothing)"
    if lab[0] == "unit":
        return f"unit {lab[1]} {lab[2]}" + (f" {lab[3]}" if lab[3] else "")
    return " ".join(str(x) for x in lab)


def diff_report(src, exp_dir, new_dir):
    lines = []
    a, b = char_labels(exp_dir), char_labels(new_dir)
    runs, cur = [], None
    for i, ch in enumerate(src):
        if ch.isspace() or a.get(i) == b.get(i):
            continue
        k = (a.get(i), b.get(i))
        if cur and cur[0] == k and i - cur[2] < 40:
            cur[2] = i
        else:
            cur = [k, i, i]
            runs.append(cur)
    if runs:
        lines.append(f"  labels: {len(runs)} changed run(s)")
        for (x, y), s, e in runs[:MAX_SHOWN]:
            t = src[s:e + 1].replace("\n", "⏎")
            lines.append(f"    [{s},{e + 1}) {show(x)}  →  {show(y)}   «{t[:70]}{'…' if len(t) > 70 else ''}»")
        if len(runs) > MAX_SHOWN:
            lines.append(f"    … {len(runs) - MAX_SHOWN} more")

    def occ(d):
        out = set()
        for x in load(os.path.join(d, "issues.json"))["issues"]:
            for o in x.get("occurrences", []):
                out.add((x["type"], o["char_start"], o["char_end"], o.get("unit_id")))
        return out
    ia, ib = occ(exp_dir), occ(new_dir)
    for tag, sset in (("issue gone", ia - ib), ("issue new ", ib - ia)):
        for t in sorted(sset, key=lambda t: t[1])[:MAX_SHOWN]:
            lines.append(f"  {tag}: {t[0]} [{t[1]},{t[2]}) {t[3] or ''}  «{src[t[1]:t[2]][:50]}»")

    pa = load(os.path.join(exp_dir, "structure.json"))["pages"]
    pb = load(os.path.join(new_dir, "structure.json"))["pages"]
    if len(pa) != len(pb):
        lines.append(f"  pages: {len(pa)} → {len(pb)}")
    for x, y in zip(pa, pb):
        if (x["char_start"], x["evidence"], x.get("footnote_zone")) != (y["char_start"], y["evidence"], y.get("footnote_zone")):
            lines.append(f"  page {x['page_index']}: start {x['char_start']} {x['evidence']} → {y['char_start']} {y['evidence']}"
                         + (f"; zone {x.get('footnote_zone')} → {y.get('footnote_zone')}" if x.get("footnote_zone") != y.get("footnote_zone") else ""))

    ua = {u["ordinal"]: u for u in load(os.path.join(exp_dir, "units.json"))["units"]}
    ub = {u["ordinal"]: u for u in load(os.path.join(new_dir, "units.json"))["units"]}
    for o in sorted(set(ua) | set(ub)):
        x, y = ua.get(o), ub.get(o)
        if not x or not y:
            lines.append(f"  unit {o}: {'added' if y else 'removed'}")
        elif (x["span"], x["confidence"], x["title"], x["number_raw"]) != (y["span"], y["confidence"], y["title"], y["number_raw"]):
            lines.append(f"  unit {o}: span/title/number/confidence {x['span']['char_start']}-{x['span']['char_end']} "
                         f"{x['confidence']} → {y['span']['char_start']}-{y['span']['char_end']} {y['confidence']}")
    if not lines:
        lines.append("  (no difference in labels, issues, pages or units — only in field details; diff the JSON files)")
    return lines


# ---------------------------------------------------------------- cases

def run_case(c, keep):
    name = c["name"]
    src_path = p(c["source"])
    tmp = tempfile.mkdtemp(prefix=f"bsa-{name}-")
    out = os.path.join(tmp, "build")
    ok, report = True, []

    # page model from the PDF (optional)
    if c.get("pdf") and c.get("pages"):
        pdf = p(c["pdf"])
        if os.path.exists(pdf):
            pj = os.path.join(tmp, "pages.json")
            rc, log = run([os.path.join(SCRIPTS, "extract_pages.py"), "--txt", src_path, "--pdf", pdf, "--out", pj])
            if rc != 0:
                ok = False
                report.append(f"  extract_pages.py failed:\n{log}")
            elif load(pj) != load(p(c["pages"])):
                ok = False
                report.append("  extract_pages.py: pages.json differs from the stored one "
                              "(re-check the layout, then store the new file with the case's snapshot)")
            else:
                report.append("  extract_pages.py: pages.json reproduced exactly")
        else:
            report.append(f"  extract_pages.py: skipped (PDF not found: {c['pdf']})")

    cmd = [os.path.join(SCRIPTS, "engine.py"), "build", "--source", src_path, "--config", p(c["config"]), "--out", out]
    if c.get("overrides"):
        cmd += ["--overrides", p(c["overrides"])]
    if c.get("pages"):
        cmd += ["--pages", p(c["pages"])]
    rc, log = run(cmd)
    if rc != 0:
        return False, report + [f"  engine.py build failed:\n{log}"], tmp
    rc, log = run([os.path.join(SCRIPTS, "validate.py"), "--source", src_path, "--out", out])
    status = log.strip().splitlines()[0] if log.strip() else "no output"
    if rc != 0:
        ok = False
        report.append(f"  validate.py: {status}")

    exp = p("expected", name)
    if not os.path.isdir(exp):
        return False, report + [f"  no snapshot in tests/expected/{name} — review the output, then --approve {name}"], tmp
    changed = [f for f in COMPARED if load(os.path.join(exp, f)) != load(os.path.join(out, f))]
    if changed:
        ok = False
        src = open(src_path, encoding="utf-8").read()
        report.append(f"  differs from snapshot: {', '.join(changed)}")
        report += diff_report(src, exp, out)
    if not keep:
        shutil.rmtree(tmp, ignore_errors=True)
    return ok, report, tmp


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--case", help="run only this case")
    ap.add_argument("--approve", metavar="CASE", help="store CASE's current output as its snapshot ('all' for every case)")
    ap.add_argument("--keep", action="store_true", help="keep the temporary build folders")
    a = ap.parse_args()
    cases = load(p("cases.json"))["cases"]

    if a.approve:
        todo = cases if a.approve == "all" else [c for c in cases if c["name"] == a.approve]
        if not todo:
            sys.exit(f"unknown case {a.approve}")
        for c in todo:
            _, report, tmp = run_case(c, keep=True)
            out = os.path.join(tmp, "build")
            if not os.path.exists(os.path.join(out, "units.json")):
                print(f"{c['name']}: build failed, nothing approved\n" + "\n".join(report))
                continue
            rc, log = run([os.path.join(SCRIPTS, "validate.py"), "--source", p(c["source"]), "--out", out])
            if rc != 0:
                print(f"{c['name']}: does not validate, nothing approved ({log.splitlines()[0]})")
                continue
            dst = p("expected", c["name"])
            os.makedirs(dst, exist_ok=True)
            for f in COMPARED + ["book_profile.json", "validation_report.json"]:
                shutil.copyfile(os.path.join(out, f), os.path.join(dst, f))
            shutil.rmtree(tmp, ignore_errors=True)
            print(f"{c['name']}: snapshot stored in tests/expected/{c['name']}")
        return

    todo = [c for c in cases if not a.case or c["name"] == a.case]
    failed = 0
    for c in todo:
        ok, report, tmp = run_case(c, a.keep)
        print(f"{'PASS' if ok else 'FAIL'}  {c['name']}  — {c.get('about', '')}")
        for line in report:
            print(line)
        if a.keep:
            print(f"  build kept in {tmp}")
        failed += not ok
    print(f"\n{len(todo) - failed}/{len(todo)} cases pass")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
