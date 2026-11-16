#!/usr/bin/env python3
"""
Regression harness for the csat restoration pipeline (phase 1).

Compares rebuilt *_restored.md files against the committed baseline files
and reports, per file, which items differ. It never writes to the baseline
or the rebuild directory; reports go to --output-dir.

Item splitting is sequential: a `N. ` header only counts as an item
boundary when N is exactly one more than the previous item number. This
avoids phantom splits from numbered lists inside passages.

Difference classification:
- expected_format: items 36, 37, 40. The phase 1 port predates the format
  decisions for these items (Q36/Q37 single joined block, Q40 passage
  only), so diffs here are recorded, not treated as regressions.
  Fixing them is phase 2 work.
- content: every other differing item. Investigated in phase 2.

Exit code is always 0: this script reports, it does not gate.
"""
import argparse
import json
import re
from pathlib import Path

HEADER_RE = re.compile(r"(?m)^(\d{1,2})\. ")
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
            "first_diff": (
                {"item": None, "line": diff[0],
                 "baseline": diff[1][:200], "rebuild": diff[2][:200]}
                if diff else None
            ),
        }
    changed = []
    for n in range(1, 46):
        if normalize(base_items[n]) != normalize(reb_items[n]):
            changed.append(n)
    if not changed:
        return {
            "status": "identical",
            "changed_items": [],
            "expected_format_items": [],
            "content_items": [],
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
    print(f"Only in rebuild: {summary['only_in_rebuild']}, "
          f"only in baseline: {summary['only_in_baseline']}")
    print(f"Reports: {output_dir / 'regression_report.json'}")
    print(f"         {output_dir / 'regression_report.md'}")


if __name__ == "__main__":
    main()
