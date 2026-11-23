#!/usr/bin/env python3
"""
Prepare step for the csat restoration pipeline (repository port, phase 1).

For each exam stem, produces the work files scripts/build.py consumes:

- work/<stem>/problem.md, answer.md  (kordoc parses)
- work/<stem>/problem_cols.txt, answer_cols.txt  (pdftotext two-column splits)
- work/<stem>/answer_full.txt  (pdftotext -layout full width, arrow source)
- work/<stem>/images/problem/*.png  (kordoc-extracted images, kordoc order)
- work/<stem>/images/pdfimages/*.png  (pdfimages extraction, PDF order)
- work/<stem>/upstage.md, upstage.response.json  (Upstage Document
  Parse, Enhanced -- only when UPSTAGE_API_KEY is set; see
  run_upstage_merge)
- work/<stem>/merged.md, merge-report.md  (hybrid merge of the
  Enhanced parse and the kordoc parse; build.py prefers merged.md
  for its Q10/Q12 table and Q25 graph extractors when it exists)

Phase 3: pdfimages output lives in its own directory. kordoc writes
images/problem/ when it parses, which used to make the pdfimages step
skip (directory non-empty) while build.py assumed kordoc order equals
pdfimages num order for page mapping. build.py now prefers
images/pdfimages/ (exact order) and falls back to images/problem/.

Ported from workspace parse_all.py, with the image extraction and
answer_full.txt steps merged in from fast_parse.py, the path actually used
for bulk preparation. Restoration behavior is unchanged: existing outputs
are skipped unless --force is given, and a non-PDF answer file leaves an
answer_not_pdf.txt marker instead of an answer key.

Stems come from Korean raw filenames (YYYY년_M월_고N_수능영어_문제.pdf),
not from a static mapping file. kordoc must be on PATH.

Image-only answer PDFs (no extractable text) get an OCR fallback: when the
pdftotext column split yields almost nothing, the answer PDF is re-parsed
with `kordoc --ocr` so the answer key table can still be recovered.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

STEM_RE = re.compile(r"^(\d{4})년_(\d{1,2})월_고([123])_수능영어_(문제|정답)\.pdf$")


def default_repo_root():
    """Repository root: parent of the directory holding this script."""
    return Path(__file__).resolve().parent.parent


def stem_and_kind(name):
    """Korean raw filename -> (stem like 2026_03_H3, kind), or None."""
    m = STEM_RE.match(name)
    if not m:
        return None
    year, month, grade, kind = m.group(1), int(m.group(2)), m.group(3), m.group(4)
    return f"{year}_{month:02d}_H{grade}", kind


def discover_pairs(raw_dir):
    """Map stem -> (problem PDF path, answer PDF path or None)."""
    found = {}
    for p in sorted(raw_dir.glob("*_수능영어_*.pdf")):
        parsed = stem_and_kind(p.name)
        if parsed is None:
            continue
        stem, kind = parsed
        entry = found.setdefault(stem, [None, None])
        entry[0 if kind == "문제" else 1] = p
    return {stem: (prob, ans) for stem, (prob, ans) in found.items() if prob is not None}


def cols_text(src, out_path):
    try:
        info = subprocess.check_output(["pdfinfo", str(src)], stderr=subprocess.DEVNULL).decode(errors="ignore")
        pages = int([l for l in info.splitlines() if l.startswith("Pages")][0].split(":")[1])
        w_total = 841
        size = [l for l in info.splitlines() if "Page size" in l]
        if size:
            try: w_total = int(float(size[0].split(":")[1].split("x")[0]))
            except Exception: pass
    except Exception:
        return False
    mid = w_total // 2
    boundary = mid
    # Adaptive column boundary: question numbers cluster at two x
    # positions (left/right column starts). Splitting at the page middle
    # clips the leading digit of right-column numbers when the text block
    # is not centred (e.g. a 612pt-wide PDF whose right column starts at
    # x=286 while mid=306: "10." reads as "0."). Detect the right-column
    # start from the most common xMin of dotted numbers in the middle
    # band and split just left of it. Falls back to mid when the layout
    # gives no clear cluster (answer PDFs, single-column pages).
    try:
        bbox = subprocess.check_output(["pdftotext", "-bbox", str(src), "-"],
                                       stderr=subprocess.DEVNULL).decode(errors="ignore")
        from collections import Counter
        xs = Counter()
        for m in re.finditer(r'<word xMin="([\d.]+)"[^>]*>(\d{1,2}\.)</word>', bbox):
            x = round(float(m.group(1)))
            if 0.3 * w_total < x < 0.7 * w_total:
                xs[x] += 1
        if xs:
            best_x, best_n = xs.most_common(1)[0]
            if best_n >= 5:
                boundary = best_x - 2
    except Exception:
        pass
    # Right crop starts at the boundary; when falling back to mid, overlap
    # 12pt leftwards so glyphs straddling the boundary are not clipped.
    right_x = boundary if boundary != mid else max(0, mid - 12)
    parts = []
    for p in range(1, pages+1):
        for x, w in [(0, boundary+1), (right_x, w_total-right_x)]:
            try:
                t = subprocess.check_output(["pdftotext","-f",str(p),"-l",str(p),"-x",str(x),"-y","0","-W",str(w),"-H","1190","-layout",str(src),"-"], stderr=subprocess.DEVNULL).decode(errors="ignore")
            except Exception:
                t = ""
            parts.append(t)
    out_path.write_text("\n".join(parts), encoding="utf-8")
    return True


def is_valid_pdf(path):
    try:
        with open(path, "rb") as f:
            return f.read(5) == b"%PDF-"
    except Exception:
        return False


def run_kordoc(kordoc, pdf, out_md, cwd, extra_args=None):
    """Run kordoc for one PDF, retrying once on failure.

    Returns True if the output file exists after. A single transient
    failure must not silently drop the parse: the retry covers the
    flaky exits seen in bulk runs, and a persistent failure prints
    kordoc's stderr tail instead of failing silently.
    """
    args = [kordoc, str(pdf), "-o", str(out_md)] + (extra_args or [])
    for attempt in range(2):
        result = subprocess.run(
            args, cwd=str(cwd),
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        if result.returncode == 0 and out_md.exists():
            return True
        err = result.stderr.decode(errors="ignore")[-400:] if result.stderr else ""
        print(f"WARNING: kordoc attempt {attempt + 1} exited "
              f"{result.returncode} for {pdf.name}: {err.strip()}",
              file=sys.stderr)
    return out_md.exists()


def extract_images(prob, img_dir):
    """Extract problem PDF images via pdfimages, in PDF order (from fast_parse.py)."""
    img_dir.mkdir(parents=True, exist_ok=True)
    tmp = img_dir.parent / "_pdfimages_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    subprocess.run(["pdfimages", "-png", str(prob), str(tmp / "img")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    files = sorted(tmp.glob("img-*.png")) + sorted(tmp.glob("img-*.jpg")) + sorted(tmp.glob("img-*.ppm")) + sorted(tmp.glob("img-*.pbm"))
    # pdfimages names are img-000, img-001... in PDF order, which matches kordoc order
    for i, f in enumerate(files, start=1):
        dest = img_dir / f"image_{i:03d}.png"
        if f.suffix.lower() == ".png":
            dest.write_bytes(f.read_bytes())
        else:
            try:
                from PIL import Image
                im = Image.open(f).convert("RGB")
                im.save(dest)
            except Exception:
                dest.write_bytes(f.read_bytes())
    manifest = [{"name": p.name, "mimeType": "image/png", "bytes": p.stat().st_size} for p in sorted(img_dir.glob("*.png"))]
    (img_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    for f in tmp.iterdir():
        f.unlink()
    tmp.rmdir()


def run_upstage_merge(stem, prob, workdir, force=False):
    """Upstage Enhanced parse + hybrid merge (new exams only).

    When UPSTAGE_API_KEY is set in the environment, parse the problem
    PDF with Upstage Document Parse (Enhanced, scripts/upstage_parse.py)
    and merge it with the kordoc parse (scripts/merge.py):

    - work/<stem>/upstage.md            (Enhanced markdown)
    - work/<stem>/upstage.response.json (full API response)
    - work/<stem>/merged.md             (hybrid merge; what build.py
                                          prefers for its table/graph
                                          extractors)
    - work/<stem>/merge-report.md       (what was overlaid / flagged)

    Without the key, or when upstage.md is already present, the API
    call is skipped and a pre-placed upstage.md is merged instead.
    Any failure here only warns: the kordoc-only path still works and
    prepare must never fail because of the optional merge.
    """
    merged_path = workdir / "merged.md"
    if merged_path.exists() and not force:
        return
    problem_md = workdir / "problem.md"
    if not problem_md.exists():
        return
    scripts_dir = Path(__file__).resolve().parent
    upstage_md = workdir / "upstage.md"
    if force or not upstage_md.exists():
        if not os.environ.get("UPSTAGE_API_KEY"):
            print(stem, "upstage merge skipped (UPSTAGE_API_KEY not set; "
                  "kordoc-only path)")
            return
        result = subprocess.run(
            [sys.executable, str(scripts_dir / "upstage_parse.py"),
             "--file", str(prob),
             "--out-md", str(upstage_md),
             "--out-json", str(workdir / "upstage.response.json")],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode != 0 or not upstage_md.exists():
            err = result.stderr.decode(errors="ignore")[-300:] if result.stderr else ""
            print(f"WARNING: {stem} upstage parse failed: {err.strip()} "
                  f"(continuing kordoc-only)", file=sys.stderr)
            return
        print(result.stdout.decode(errors="ignore").strip())
    result = subprocess.run(
        [sys.executable, str(scripts_dir / "merge.py"),
         "--enhanced", str(upstage_md),
         "--kordoc", str(problem_md),
         "--out", str(merged_path),
         "--report", str(workdir / "merge-report.md")],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0 or not merged_path.exists():
        err = result.stderr.decode(errors="ignore")[-300:] if result.stderr else ""
        print(f"WARNING: {stem} hybrid merge failed: {err.strip()} "
              f"(continuing kordoc-only)", file=sys.stderr)
        return
    print(stem, "upstage merge done ->", merged_path.name)


def parse_one(stem, prob, ans, work_dir, kordoc, force=False, upstage=True):
    workdir = work_dir / stem
    workdir.mkdir(parents=True, exist_ok=True)
    # kordoc problem
    if force or not (workdir / "problem.md").exists():
        run_kordoc(kordoc, prob, workdir / "problem.md", workdir)
    # Upstage Enhanced + hybrid merge (optional, key-gated; see above).
    if upstage:
        run_upstage_merge(stem, prob, workdir, force=force)
    if force or not (workdir / "problem_cols.txt").exists():
        cols_text(prob, workdir / "problem_cols.txt")
    # images via pdfimages into their own directory (phase 3: never
    # share kordoc's images/problem/, whose non-empty state used to
    # skip this step entirely).
    img_dir = workdir / "images" / "pdfimages"
    if force or not img_dir.exists() or not any(img_dir.iterdir()):
        extract_images(prob, img_dir)
    # answer: only if PDF
    is_pdf = ans is not None and is_valid_pdf(ans)
    if is_pdf:
        if force or not (workdir / "answer.md").exists():
            run_kordoc(kordoc, ans, workdir / "answer.md", workdir)
        if force or not (workdir / "answer_cols.txt").exists():
            cols_text(ans, workdir / "answer_cols.txt")
        if force or not (workdir / "answer_full.txt").exists():
            try:
                t = subprocess.check_output(["pdftotext","-layout",str(ans),"-"], stderr=subprocess.DEVNULL).decode(errors="ignore")
                (workdir / "answer_full.txt").write_text(t, encoding="utf-8")
            except Exception:
                pass
        # OCR fallback for image-only answer PDFs: when the pdftotext
        # extraction yields almost nothing, re-parse with --ocr so the
        # answer key table can be recovered from the rendered pages.
        # Guarded by both sizes so text PDFs are never re-parsed.
        cols_path = workdir / "answer_cols.txt"
        cols_size = cols_path.stat().st_size if cols_path.exists() else 0
        ans_md = workdir / "answer.md"
        md_size = ans_md.stat().st_size if ans_md.exists() else 0
        if cols_size < 200 and md_size < 2000:
            run_kordoc(kordoc, ans, ans_md, workdir, extra_args=["--ocr"])
            print(stem, "answer OCR fallback applied")
    else:
        (workdir / "answer_not_pdf.txt").write_text("answer file is not a PDF (HTML), no answer key locally\n", encoding="utf-8")
    print(stem, "done", "answer_pdf" if is_pdf else "answer_HTML")


def main():
    ap = argparse.ArgumentParser(description="Prepare per-exam parse files for scripts/build.py.")
    ap.add_argument("--repo-root", default=None,
                    help="repository root (default: parent of scripts/)")
    ap.add_argument("--raw-dir", default=None,
                    help="raw PDF directory (default: <repo-root>/data/raw)")
    ap.add_argument("--work-dir", default=None,
                    help="work directory (default: <repo-root>/.restore-work)")
    ap.add_argument("--only", nargs="*", default=None, help="stems to prepare, e.g. 2026_03_H3")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="redo all steps even when outputs already exist")
    ap.add_argument("--no-upstage", action="store_true",
                    help="skip the Upstage parse + hybrid merge even when "
                         "UPSTAGE_API_KEY is set (kordoc-only path)")
    args = ap.parse_args()

    repo_root = Path(args.repo_root).resolve() if args.repo_root else default_repo_root()
    raw_dir = Path(args.raw_dir).resolve() if args.raw_dir else repo_root / "data" / "raw"
    work_dir = Path(args.work_dir).resolve() if args.work_dir else repo_root / ".restore-work"

    pairs = discover_pairs(raw_dir)
    stems = sorted(pairs)
    if args.only:
        stems = [s for s in stems if s in args.only]
    if args.list:
        print("\n".join(stems))
        return

    kordoc = shutil.which("kordoc")
    if kordoc is None:
        print("error: kordoc not found on PATH. Install it first "
              "(npm install -g kordoc) and make sure it is on PATH.",
              file=sys.stderr)
        sys.exit(2)

    work_dir.mkdir(parents=True, exist_ok=True)
    for s in stems:
        prob, ans = pairs[s]
        try:
            parse_one(s, prob, ans, work_dir, kordoc, force=args.force,
                      upstage=not args.no_upstage)
        except Exception as e:
            print(s, "ERROR", e)


if __name__ == "__main__":
    main()
