#!/usr/bin/env python3
"""
run_tests.py — tests for the knowledge-extractor scripts. Run after ANY change to prepare.py,
check.py, common.py or the prompt.

For every book in tests/cases.json, built on the APPROVED output of book-structure-analyzer
(its tests/expected snapshots — the same data both skills are tested on):
  1. sheets    prepare.py makes the sample units' sheets; they must equal the snapshot
               (drafts cite piece ids like p02 — a silent change there would misplace every quote)
  2. good      check.py on the reviewed good drafts must accept everything, and
               knowledge_objects.json must equal the snapshot (same objects, same exact offsets)
  3. bad       every planted mistake in fixtures/<book>/bad.json must be rejected with its code

  python tests/run_tests.py                  run everything
  python tests/run_tests.py --approve BOOK   store BOOK's current sheets and knowledge_objects.json as
                                             the snapshot ('all' for every book) — only after the user
                                             has reviewed the differences
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")


def p(*parts):
    return os.path.normpath(os.path.join(HERE, *parts))


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def run(args):
    r = subprocess.run([sys.executable] + args, capture_output=True, text=True, encoding="utf-8",
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def prepare(c, out):
    return run([os.path.join(SCRIPTS, "prepare.py"), "--source", p(c["source"]), "--build", p(c["build"]),
                "--config", p(c["config"]), "--out", out, "--units", ",".join(c["units"])])


def check(c, sheets, drafts, out, units):
    return run([os.path.join(SCRIPTS, "check.py"), "--sheets", sheets, "--drafts", drafts, "--config", p(c["config"]),
                "--out", out, "--units", ",".join(units)])


def test_book(c, tmp):
    fails, notes = [], []
    sheets = os.path.join(tmp, "sheets")
    rc, log = prepare(c, sheets)
    if rc:
        return [f"prepare.py failed: {log}"], notes
    exp = p("expected", c["name"])
    for u in c["units"]:
        snap = os.path.join(exp, "sheets", f"{u}.json")
        if not os.path.exists(snap):
            fails.append(f"no sheet snapshot for {u} — review, then --approve {c['name']}")
        elif load(snap) != load(os.path.join(sheets, f"{u}.json")):
            fails.append(f"sheet {u} differs from the snapshot (piece ids or texts changed)")

    # good drafts
    ko = os.path.join(tmp, "knowledge_objects.json")
    rc, log = check(c, sheets, p(c["good"]), ko, c["units"])
    if rc:
        fails.append("good drafts were rejected:\n    " + "\n    ".join(l for l in log.splitlines() if "REJECTED" in l))
    elif not os.path.exists(os.path.join(exp, "knowledge_objects.json")):
        fails.append(f"no knowledge_objects snapshot — review, then --approve {c['name']}")
    elif load(ko) != load(os.path.join(exp, "knowledge_objects.json")):
        a, b = load(os.path.join(exp, "knowledge_objects.json"))["objects"], load(ko)["objects"]
        fails.append("knowledge_objects.json differs from the snapshot")
        for x, y in zip(a, b):
            if x != y:
                fails.append(f"  first difference at {x['id']}: {json.dumps(x, ensure_ascii=False)[:160]} → "
                             f"{json.dumps(y, ensure_ascii=False)[:160]}")
                break
        if len(a) != len(b):
            fails.append(f"  {len(a)} objects → {len(b)}")
    else:
        notes.append(f"good drafts: {len(load(ko)['objects'])} objects accepted, identical to the snapshot")

    # planted mistakes
    bad = load(p(c["bad"]))
    uid = bad["unit_id"]
    caught = 0
    for case in bad["cases"]:
        d = os.path.join(tmp, "bad")
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)
        with open(os.path.join(d, f"{uid}.json"), "w", encoding="utf-8") as f:
            if "raw" in case:
                f.write(case["raw"])
            else:
                draft = case.get("draft") or {"unit_id": uid, "model_used": "test", "prompt_version": "ke-v1",
                                              "no_knowledge_reason": None, "objects": [case["object"]]}
                json.dump(draft, f, ensure_ascii=False)
        out = os.path.join(tmp, "bad_ko.json")
        rc, log = check(c, sheets, d, out, [uid])
        res = load(out) if os.path.exists(out) else {"objects": [], "rejected": []}
        codes = {r.split(":")[0] for x in res["rejected"] for r in x["reasons"]}
        missing = [e for e in case["expect"] if e not in codes]
        if rc == 0 or res["objects"] or missing:
            fails.append(f"planted mistake NOT caught — {case['name']}: expected {case['expect']}, got {sorted(codes) or 'accepted'}")
        else:
            caught += 1
    notes.append(f"planted mistakes: {caught}/{len(bad['cases'])} rejected with the right code")
    return fails, notes


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--approve", metavar="BOOK")
    a = ap.parse_args()
    cases = load(p("cases.json"))["books"]

    if a.approve:
        for c in cases:
            if a.approve not in ("all", c["name"]):
                continue
            tmp = tempfile.mkdtemp(prefix="ke-")
            sheets = os.path.join(tmp, "sheets")
            rc, log = prepare(c, sheets)
            ko = os.path.join(tmp, "knowledge_objects.json")
            rc2, log2 = check(c, sheets, p(c["good"]), ko, c["units"]) if not rc else (1, log)
            if rc or rc2:
                print(f"{c['name']}: not approved — {log if rc else log2}")
                continue
            exp = p("expected", c["name"])
            shutil.rmtree(exp, ignore_errors=True)
            shutil.copytree(sheets, os.path.join(exp, "sheets"))
            shutil.copyfile(ko, os.path.join(exp, "knowledge_objects.json"))
            shutil.rmtree(tmp, ignore_errors=True)
            print(f"{c['name']}: snapshot stored in tests/expected/{c['name']}")
        return

    failed = 0
    for c in cases:
        tmp = tempfile.mkdtemp(prefix="ke-")
        fails, notes = test_book(c, tmp)
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"{'PASS' if not fails else 'FAIL'}  {c['name']}")
        for n in notes:
            print(f"  {n}")
        for f in fails:
            print(f"  FAIL: {f}")
        failed += bool(fails)
    print(f"\n{len(cases) - failed}/{len(cases)} books pass")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
