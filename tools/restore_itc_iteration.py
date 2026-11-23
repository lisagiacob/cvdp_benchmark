#!/usr/bin/env python3
"""
Restore the on-disk agent output of an --iter-to-convergence datapoint to the
state it had at the end of iteration K, so it can be re-graded (e.g. with
tools/resume_agentic_eval.py --only-eval --refresh-harness).

The loop saves harness/<n>/agent_changes_iter<K>.patch: a difflib unified diff
of rtl/verif/docs against the pre-agent snapshot harness/<n>/before/, built
from str.splitlines() and joined with blank lines. GNU patch misreads it
(form feeds / \\r split lines, the blank separators), so the hunks are applied
here by their header line counts.

Usage:
    python tools/restore_itc_iteration.py --prefix <work_dir> --iteration 1 \\
        --only-ids id1,id2,... [--check] [--dry-run]

--check rebuilds the LAST iteration's patch into a temp dir and compares it with
the current on-disk state (whitespace-insensitive), to validate the method
without touching anything.
"""

import argparse
import os
import re
import shutil
import sys
import tempfile

DIRS = ("rtl", "verif", "docs")
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def issue_path(prefix, id_str):
    m = re.match(r"(.*)_(\d+)$", id_str)
    return os.path.join(prefix, m.group(1), "harness", str(int(m.group(2))))


def parse_patch(text):
    """{path: [(old_start, old_len, new_lines_of_hunk, old_lines_of_hunk)]}"""
    lines = text.split("\n")
    files, i, cur = {}, 0, None
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- a/") and i + 1 < len(lines) and lines[i + 1].startswith("+++ b/"):
            cur = lines[i + 1][len("+++ b/"):]
            files[cur] = []
            i += 2
            continue
        m = HUNK.match(line)
        if m and cur is not None:
            o_start, o_len = int(m.group(1)), int(m.group(2) or 1)
            n_len = int(m.group(4) or 1)
            old, new, i = [], [], i + 1
            while len(old) < o_len or len(new) < n_len:
                body = lines[i]
                tag, content = (body[:1], body[1:]) if body else (" ", "")
                if tag in (" ", "-"):
                    old.append(content)
                if tag in (" ", "+"):
                    new.append(content)
                i += 1
            files[cur].append((o_start, o_len, old, new))
            continue
        i += 1
    return files


def read_lines(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read().splitlines()


def apply(before_lines, hunks, path):
    out, pos = [], 0
    for o_start, o_len, old, new in hunks:
        start = o_start - 1 if o_len else o_start
        if before_lines[start:start + o_len] != old:
            raise ValueError(f"{path}: context mismatch at line {o_start}")
        out += before_lines[pos:start] + new
        pos = start + o_len
    return out + before_lines[pos:]


def rebuild(ipath, patch_file, dest):
    """Write before/ + patch into dest/{rtl,verif,docs}. Returns the files touched."""
    for d in DIRS:
        if os.path.isdir(os.path.join(ipath, "before", d)):
            shutil.copytree(os.path.join(ipath, "before", d), os.path.join(dest, d))
    with open(patch_file, "r", encoding="utf-8", errors="replace") as f:
        files = parse_patch(f.read())
    for rel, hunks in files.items():
        new = apply(read_lines(os.path.join(ipath, "before", rel)), hunks, rel)
        target = os.path.join(dest, rel)
        if not new:  # deleted by the agent (difflib diffs it against "")
            if os.path.exists(target):
                os.remove(target)
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write("\n".join(new) + "\n")
    return sorted(files)


def same_tree(a, b):
    def norm(path):
        return [l.rstrip() for l in read_lines(path) if l.strip()]
    for d in DIRS:
        fa = {os.path.relpath(os.path.join(r, f), os.path.join(a, d))
              for r, _, fs in os.walk(os.path.join(a, d)) for f in fs}
        fb = {os.path.relpath(os.path.join(r, f), os.path.join(b, d))
              for r, _, fs in os.walk(os.path.join(b, d)) for f in fs}
        for rel in fa | fb:
            pa, pb = os.path.join(a, d, rel), os.path.join(b, d, rel)
            if norm(pa) != norm(pb):
                return False, os.path.join(d, rel)
    return True, None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prefix", required=True)
    ap.add_argument("--only-ids", required=True, help="Comma-separated dataset ids")
    ap.add_argument("--iteration", type=int, default=1)
    ap.add_argument("--check", action="store_true", help="Validate on the last iteration, touch nothing")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    failed = []
    for id_str in [i.strip() for i in args.only_ids.split(",") if i.strip()]:
        ipath = issue_path(args.prefix, id_str)
        if args.check:
            ks = [int(m.group(1)) for f in os.listdir(ipath)
                  if (m := re.match(r"agent_changes_iter(\d+)\.patch$", f))]
            k = max(ks)
        else:
            k = args.iteration
        patch_file = os.path.join(ipath, f"agent_changes_iter{k}.patch")
        if not os.path.exists(patch_file):
            print(f"  [SKIP] {id_str}: no {os.path.basename(patch_file)}")
            failed.append(id_str)
            continue
        tmp = tempfile.mkdtemp(prefix="itc_restore_")
        try:
            touched = rebuild(ipath, patch_file, tmp)
            if args.check:
                ok, where = same_tree(tmp, ipath)
                print(f"  [CHECK] {id_str}: iter{k} patch {'== on-disk state' if ok else '!= on-disk state at ' + where}")
                if not ok:
                    failed.append(id_str)
                continue
            print(f"  [RESTORE] {id_str}: before/ + iter{k} patch ({len(touched)} file(s))"
                  + (" [dry-run]" if args.dry_run else ""))
            if not args.dry_run:
                for d in DIRS:
                    live = os.path.join(ipath, d)
                    if os.path.isdir(os.path.join(tmp, d)):
                        if os.path.isdir(live):
                            shutil.rmtree(live)
                        shutil.copytree(os.path.join(tmp, d), live)
        except Exception as e:
            print(f"  [ERROR] {id_str}: {e}")
            failed.append(id_str)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    if failed:
        sys.exit(f"\n{len(failed)} id(s) failed: {','.join(failed)}")


if __name__ == "__main__":
    main()
