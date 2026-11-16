#!/usr/bin/env python3
"""
Regression harness for the csat restoration pipeline (phase 2).

Compares rebuilt *_restored.md files against the committed baseline files
and reports, per file, which items differ. It never writes to the baseline
or the rebuild directory; reports go to --output-dir.

Item splitting is sequential: a `N. ` header only counts as an item
boundary when N is exactly one more than the previous item number. This
avoids phantom splits from numbered lists inside passages.

Difference classification is structural only: every cause is computed
from the two item texts and the item number, with no audit evidence and
no grade assignment (grades live in the local triage tooling, not in
this harness):

- whitespace_only: equal after whitespace normalization.
- image_only: equal once image references are stripped; only the
  image refs differ (phase-3 image assignment).
- graph_table: a Q25 graph item whose baseline table the rebuild
  lacks (phase-3 graph tables).
- notice_layout: a Q27/28 item with the same words modulo line
  breaks, bullets, dashes, colons, and Hangul spacing.
- spacing_only: equal once all whitespace is removed (column-break
  word splits, particle spacing).
- prompt_only: only the first (prompt) line differs.
- small_word_diff: a few small token edits (a correction-queue
  candidate or a wording difference; grade assignment needs audit
  evidence and is not done here).
- segment_order: a Q36/37/43/44/45 item whose diff is not explained
  by the above (segment order or segment content).
- other: anything else; must be investigated, never assumed benign.

The expected_format/content qnum buckets from phase 1 are kept for
continuity; the Q36/Q37/Q40 format decisions are implemented since
phase 2, so those items are classified structurally like the rest.

Exit code is always 0: this script reports, it does not gate.
"""
import argparse
import difflib
import json
import re
from collections import Counter
from pathlib import Path

HEADER_RE = re.compile(r"(?m)^(\d{1,2})\. ")
IMG_RE = re.compile(r"!\[image\]\(([^)]+)\)")
BULLETS = set("⦁∙◈▪ㆍ⚫✓✔※•□·")
EXPECTED_FORMAT_ITEMS = {36, 37, 40}


def split_items(text):
    """Split restored markdown into {item_number: body} sequentially.

    Returns (items, ok): ok is False when the file does not split into
    exactly items 1..45.
    """
    matches = list(HEADER_RE.finditer(text))
    accepted = []
    expected = 1
    for m in matches:
        if int(m.group(1)) == expected:
            accepted.append(m)
            expected += 1
            if expected > 45:
                break
    if len(accepted) != 45:
        return {}, False
    items = {}
    for i, m in enumerate(accepted):
        end = accepted[i + 1].start() if i + 1 < len(accepted) else len(text)
        items[int(m.group(1))] = text[m.start():end]
    return items, True


def normalize(body):
    lines = [ln.rstrip() for ln in body.splitlines()]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def first_line_diff(a_lines, b_lines):
    """First differing line index and snippets, or None when equal."""
    for i, (a, b) in enumerate(zip(a_lines, b_lines)):
        if a != b:
            return i, a, b
    if len(a_lines) != len(b_lines):
        i = min(len(a_lines), len(b_lines))
        a = a_lines[i] if i < len(a_lines) else "<end of item>"
        b = b_lines[i] if i < len(b_lines) else "<end of item>"
        return i, a, b
    return None


def _ws_norm(s):
    return re.sub(r"\s+", " ", s).strip()


def classify_item(n, base_body, reb_body):
    """Structural cause for one differing item (see module docstring)."""
    if _ws_norm(base_body) == _ws_norm(reb_body):
        return "whitespace_only"
    b_ni = _ws_norm(IMG_RE.sub("", base_body))
    r_ni = _ws_norm(IMG_RE.sub("", reb_body))
    if b_ni == r_ni:
        return "image_only"
    prompt_b = base_body.splitlines()[0] if base_body.splitlines() else ""
    if n == 25 or "도표" in prompt_b:
        b_table = [l for l in base_body.splitlines() if l.lstrip().startswith("|")]
        r_table = [l for l in reb_body.splitlines() if l.lstrip().startswith("|")]
        if b_table and not r_table:
            return "graph_table"
    if n in (27, 28):
        def bag(s):
            s = re.sub(r"(?<=[가-힣]) (?=[가-힣])", "", IMG_RE.sub("", s))
            toks = []
            for t in _ws_norm(s).split(" "):
                t = t.strip(":.,!?;\"”’'").strip("".join(BULLETS))
                if t and t not in ("-", "‐", "–"):
                    toks.append(t)
            return Counter(toks)
        if bag(base_body) == bag(reb_body):
            return "notice_layout"
    if re.sub(r"\s+", "", b_ni) == re.sub(r"\s+", "", r_ni):
        return "spacing_only"
    b_lines = [l for l in base_body.splitlines() if not IMG_RE.search(l)]
    r_lines = [l for l in reb_body.splitlines() if not IMG_RE.search(l)]
    if len(b_lines) == len(r_lines) and b_lines[1:] == r_lines[1:]:
        return "prompt_only"
    b_tok, r_tok = b_ni.split(" "), r_ni.split(" ")
    sm = difflib.SequenceMatcher(None, b_tok, r_tok, autojunk=False)
    ops = [o for o in sm.get_opcodes() if o[0] != "equal"]
    if ops and len(ops) <= 3 and all(
            o[0] in ("replace", "insert", "delete")
            and (o[2] - o[1]) <= 3 and (o[4] - o[3]) <= 3 for o in ops):
        return "small_word_diff"
    if n in (36, 37, 43, 44, 45):
        return "segment_order"
    return "other"


def compare_files(baseline_text, rebuild_text):
    """Compare one file pair. Returns a per-file result dict."""
    base_items, base_ok = split_items(baseline_text)
    reb_items, reb_ok = split_items(rebuild_text)
    if not base_ok or not reb_ok:
        # Fall back to a whole-file line diff.
        a_lines = normalize(baseline_text).splitlines()
        b_lines = normalize(rebuild_text).splitlines()
        diff = first_line_diff(a_lines, b_lines)
        return {
            "status": "structure_mismatch",
            "baseline_items_ok": base_ok,
            "rebuild_items_ok": reb_ok,
            "changed_items": [],
            "expected_format_items": [],
            "content_items": [],
            "item_causes": {},
            "first_diff": (
                {"item": None, "line": diff[0],
                 "baseline": diff[1][:200], "rebuild": diff[2][:200]}
                if diff else None
            ),
        }
    changed = []
    causes = {}
    for n in range(1, 46):
        if normalize(base_items[n]) != normalize(reb_items[n]):
            changed.append(n)
            causes[n] = classify_item(n, base_items[n], reb_items[n])
    if not changed:
        return {
            "status": "identical",
            "changed_items": [],
            "expected_format_items": [],
            "content_items": [],
            "item_causes": {},
            "first_diff": None,
        }
    expected = [n for n in changed if n in EXPECTED_FORMAT_ITEMS]
    content = [n for n in changed if n not in EXPECTED_FORMAT_ITEMS]
    first = changed[0]
    diff = first_line_diff(
        normalize(base_items[first]).splitlines(),
        normalize(reb_items[first]).splitlines(),
    )
    return {
        "status": "changed",
        "changed_items": changed,
        "expected_format_items": expected,
        "content_items": content,
        "item_causes": causes,
        "first_diff": (
            {"item": first, "line": diff[0],
             "baseline": diff[1][:200], "rebuild": diff[2][:200]}
            if diff else None
        ),
    }


def collect(md_dir):
    return {
        str(p.relative_to(md_dir)): p
        for p in sorted(md_dir.rglob("*_restored.md"))
    }


def build_markdown(report):
    lines = ["# Regression report", ""]
    s = report["summary"]
    lines.append(f"- Files compared: {s['compared']}")
    lines.append(f"- Identical: {s['identical']}")
    lines.append(f"- Changed: {s['changed']}")
    lines.append(f"- Structure mismatch: {s['structure_mismatch']}")
    lines.append(f"- Only in rebuild (new files): {s['only_in_rebuild']}")
    lines.append(f"- Only in baseline (not rebuilt): {s['only_in_baseline']}")
    lines.append(f"- Changed items total: {s['changed_items_total']} "
                 f"(expected format: {s['expected_format_items_total']}, "
                 f"content: {s['content_items_total']})")
    lines.append("")
    lines.append("## Changed items by structural cause")
    lines.append("")
    for cause, n in sorted(s["causes"].items()):
        lines.append(f"- {cause}: {n}")
    lines.append("")
    lines.append("## Per-file results")
    lines.append("")
    lines.append("| File | Status | Changed items | Expected format | Content |")
    lines.append("| --- | --- | --- | --- | --- |")
    for rel, r in report["files"].items():
        lines.append(
            f"| {rel} | {r['status']} | {len(r['changed_items'])} | "
            f"{len(r['expected_format_items'])} | {len(r['content_items'])} |"
        )
    lines.append("")
    lines.append("## First diffs (changed files)")
    lines.append("")
    any_detail = False
    for rel, r in report["files"].items():
        if r["status"] not in ("changed", "structure_mismatch"):
            continue
        any_detail = True
        d = r["first_diff"]
        lines.append(f"### {rel}")
        lines.append("")
        lines.append(f"- Changed items: {r['changed_items']}")
        if d:
            lines.append(f"- First diff at item {d['item']}, line {d['line']}:")
            lines.append(f"  - baseline: `{d['baseline']}`")
            lines.append(f"  - rebuild: `{d['rebuild']}`")
        lines.append("")
    if not any_detail:
        lines.append("No differences found.")
        lines.append("")
    if report["only_in_rebuild"]:
        lines.append("## Only in rebuild")
        lines.append("")
        for rel in report["only_in_rebuild"]:
            lines.append(f"- {rel}")
        lines.append("")
    if report["only_in_baseline"]:
        lines.append("## Only in baseline (not rebuilt in this run)")
        lines.append("")
        for rel in report["only_in_baseline"]:
            lines.append(f"- {rel}")
        lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(
        description="Compare rebuilt restored-md files against the baseline.")
    ap.add_argument("--baseline-dir", required=True,
                    help="directory tree with committed *_restored.md files")
    ap.add_argument("--rebuild-dir", required=True,
                    help="directory tree with rebuilt *_restored.md files")
    ap.add_argument("--output-dir", default="regression-report",
                    help="where to write regression_report.json/.md")
    args = ap.parse_args()

    baseline_dir = Path(args.baseline_dir)
    rebuild_dir = Path(args.rebuild_dir)
    output_dir = Path(args.output_dir)

    baseline = collect(baseline_dir)
    rebuild = collect(rebuild_dir)

    files = {}
    for rel in sorted(set(baseline) & set(rebuild)):
        files[rel] = compare_files(
            baseline[rel].read_text(encoding="utf-8"),
            rebuild[rel].read_text(encoding="utf-8"),
        )
    only_in_rebuild = sorted(set(rebuild) - set(baseline))
    only_in_baseline = sorted(set(baseline) - set(rebuild))

    summary = {
        "compared": len(files),
        "identical": sum(1 for r in files.values() if r["status"] == "identical"),
        "changed": sum(1 for r in files.values() if r["status"] == "changed"),
        "structure_mismatch": sum(1 for r in files.values() if r["status"] == "structure_mismatch"),
        "only_in_rebuild": len(only_in_rebuild),
        "only_in_baseline": len(only_in_baseline),
        "changed_items_total": sum(len(r["changed_items"]) for r in files.values()),
        "expected_format_items_total": sum(len(r["expected_format_items"]) for r in files.values()),
        "content_items_total": sum(len(r["content_items"]) for r in files.values()),
        "causes": dict(sorted(Counter(
            cause for r in files.values() for cause in r["item_causes"].values()
        ).items())),
    }
    report = {
        "baseline_dir": str(baseline_dir),
        "rebuild_dir": str(rebuild_dir),
        "summary": summary,
        "files": files,
        "only_in_rebuild": only_in_rebuild,
        "only_in_baseline": only_in_baseline,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "regression_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "regression_report.md").write_text(
        build_markdown(report), encoding="utf-8")

    print(f"Compared {summary['compared']} files: "
          f"{summary['identical']} identical, {summary['changed']} changed, "
          f"{summary['structure_mismatch']} structure mismatch")
    print(f"Changed items: {summary['changed_items_total']} "
          f"(expected format {summary['expected_format_items_total']}, "
          f"content {summary['content_items_total']})")
    print("Causes: " + ", ".join(f"{k} {v}" for k, v in summary["causes"].items()))
    print(f"Only in rebuild: {summary['only_in_rebuild']}, "
          f"only in baseline: {summary['only_in_baseline']}")
    print(f"Reports: {output_dir / 'regression_report.json'}")
    print(f"         {output_dir / 'regression_report.md'}")


if __name__ == "__main__":
    main()
