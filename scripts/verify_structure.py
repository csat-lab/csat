#!/usr/bin/env python3
"""
Structural verification for restored exam markdown (repository port, phase 1).

Ported from workspace verify.py. Checks, per *_restored.md file:
- question numbers are exactly 1..45 in order
- no dash before circled option numbers
- no forbidden words (복원 메모, 부록, 정답표)
- every referenced image file exists next to the markdown
- questions 1-17 have at least one circled option (except picture questions)
- questions 18-45 have no circled numbers left in the passage

This is the structural check. The recall-based check against the original
parses lives in scripts/verify_restoration.py.
"""
import argparse
import json
import re
import sys
from pathlib import Path


def default_repo_root():
    """Repository root: parent of the directory holding this script."""
    return Path(__file__).resolve().parent.parent


def verify_dir(input_dir):
    issues = []
    total = 0
    per_grade = {}
    for md in sorted(input_dir.rglob("*_restored.md")):
        total += 1
        grade = md.parent.name
        per_grade[grade] = per_grade.get(grade, 0) + 1
        text = md.read_text(encoding="utf-8")
        qs = re.findall(r"(?m)^(\d+)\. ", text)
        if qs != [str(i) for i in range(1, 46)]:
            issues.append(f"{md.name}: question numbers {qs[:5]}...{qs[-5:]} count {len(qs)}")
        if re.search(r"(?m)^\s*-\s*[①②③④⑤]", text):
            issues.append(f"{md.name}: dash before circled")
        for w in ["복원 메모", "부록", "정답표"]:
            if w in text:
                issues.append(f"{md.name}: contains {w}")
        # image references exist?
        for img in re.findall(r"!\[image\]\(([^)]+)\)", text):
            if not (md.parent / img).exists():
                issues.append(f"{md.name}: missing image file {img}")
        # 1-17 options: at least one circled per question except Q4 pic (allow 0 for pic)
        parts = re.split(r"(?m)^(\d+)\. ", text)
        d = {}
        for i in range(1, len(parts), 2):
            d[int(parts[i])] = parts[i+1]
        for n in range(1, 18):
            seg = d.get(n, "")
            if "그림" in seg.splitlines()[0] if seg.splitlines() else False:
                continue
            if not re.search(r"[①②③④⑤]", seg):
                issues.append(f"{md.name}: Q{n} no options")
        for n in range(18, 46):
            seg = d.get(n, "")
            seg2 = re.sub(r"!\[image\]\([^)]+\)", "", seg)
            # allow circled in passage? No, 18-45 should have no options, but passage may contain circled markers if not stripped - flag
            if re.search(r"[①②③④⑤]", seg2):
                issues.append(f"{md.name}: Q{n} still has circled numbers")
    return {"total": total, "per_grade": per_grade, "issues": issues}


def main():
    ap = argparse.ArgumentParser(description="Structural check for *_restored.md files.")
    ap.add_argument("--repo-root", default=None,
                    help="repository root (default: parent of scripts/)")
    ap.add_argument("--input-dir", default=None,
                    help="directory tree holding *_restored.md (default: <repo-root>/data/output)")
    ap.add_argument("--report", default=None,
                    help="write the JSON report to this path (default: print only)")
    ap.add_argument("--fail-on-issues", action="store_true",
                    help="exit 1 when any issue is found (default: exit 0)")
    args = ap.parse_args()

    repo_root = Path(args.repo_root) if args.repo_root else default_repo_root()
    input_dir = Path(args.input_dir) if args.input_dir else repo_root / "data" / "output"

    result = verify_dir(input_dir)
    print(f"Total md: {result['total']}, per grade: {result['per_grade']}")
    print(f"Issues: {len(result['issues'])}")
    for e in result["issues"][:300]:
        print(" -", e)
    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.fail_on_issues and result["issues"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
