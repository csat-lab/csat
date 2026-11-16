#!/usr/bin/env python3
"""Strict graph/data-table extraction for CSAT restoration (phase 3).

Extracts Q25 data tables from the kordoc problem parse (the PDF's own
text layer) -- never from OCR, never invented. A table is emitted only
when it parses completely: consistent columns, textual headers, and
labelled data rows. Anything else (bar-chart axis junk, fragmented
cells, clipped titles) is rejected, logged, and queued for manual
transcription.

This module deliberately does NOT reuse the OCR approach of the local
graph_table.py: OCR yields flattened numbers without reliable label
association, and generic labels ("Category 1") would fabricate content.
"""
import re

HDR_RE = re.compile(r"^\s*(\d{1,2})\.\s")
STEM_MARKER_RE = re.compile(
    r"The\s+(?:tables?\s+above|above\s+(?:tables?|graphs?|charts?))\b")

# Lines that can never be a table title.
_TITLE_SKIP = ("#", "※", "|", "<")


def _q25_region(problem_md_text):
    """Lines of the Q25 region: from the first '25.' header line to the
    next later item header. Kordoc headers are not a clean 1..45
    sequence (group headers, dotted variants), so no sequential
    assumption is made; only the Q25 boundaries matter here."""
    lines = problem_md_text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if re.match(r"^\s*25\.\s", ln):
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if re.match(r"^\s*(2[6-9]|[3-4][0-5])\.\s", lines[i]):
            end = i
            break
    return lines[start:end]


def _regions(problem_md_text):
    """Kept for compatibility: {25: region lines} when Q25 is found."""
    region = _q25_region(problem_md_text)
    return {25: region} if region is not None else {}


def _clean_cell(text):
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = re.sub(r"\s+", " ", text).strip()
    # kordoc spaces parenthetical letters: "( A )" -> "(A)"
    text = re.sub(r"\(\s*([A-Za-z0-9])\s*\)", r"(\1)", text)
    return text


def _split_pipe_row(line):
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return [_clean_cell(c) for c in cells]


def _is_separator_row(line):
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c) for c in cells)


def _parse_pipe_blocks(region_lines):
    """Yield (start_idx, end_idx, rows) for each pipe-table block."""
    i = 0
    n = len(region_lines)
    while i < n:
        ln = region_lines[i]
        if ln.strip().startswith("|") and i + 1 < n and _is_separator_row(region_lines[i + 1]):
            rows = [_split_pipe_row(ln)]
            j = i + 2
            while j < n and region_lines[j].strip().startswith("|") and not _is_separator_row(region_lines[j]):
                rows.append(_split_pipe_row(region_lines[j]))
                j += 1
            yield i, j, rows
            i = j
        else:
            i += 1


def _parse_html_table(block_text):
    """Parse one <table> block into a grid, expanding colspan/rowspan.

    Colspan in the first (header) row repeats the spanning text into
    each covered column, so a two-row header flattens symmetrically
    ('Share ...' over both 2018 and 2020). Colspan elsewhere fills
    with empty cells (a spanned 'Overall Total' must not duplicate).
    Rowspan continuations become empty stubs, so header merging joins
    the upper cell with the lower row's label.
    """
    raw_rows = []
    for tr in re.finditer(r"<tr[^>]*>(.*?)</tr>", block_text, flags=re.S | re.I):
        cells = []
        for cell in re.finditer(r"<t[hd]([^>]*)>(.*?)</t[hd]>", tr.group(1), flags=re.S | re.I):
            attrs, inner = cell.group(1), cell.group(2)
            cm = re.search(r'colspan\s*=\s*"?(\d+)', attrs, flags=re.I)
            rm = re.search(r'rowspan\s*=\s*"?(\d+)', attrs, flags=re.I)
            text = _clean_cell(re.sub(r"<br\s*/?>", " ", inner, flags=re.I))
            cells.append((text, max(1, int(rm.group(1))) if rm else 1,
                          max(1, int(cm.group(1))) if cm else 1))
        if cells:
            raw_rows.append(cells)
    if not raw_rows:
        return None
    grid = []
    pending = {}  # col -> rows remaining (rowspan stubs)
    for ri, cells in enumerate(raw_rows):
        row = []
        col = 0
        ci = 0
        while ci < len(cells) or any(p >= col for p in pending):
            if col in pending:
                row.append("")
                pending[col] -= 1
                if pending[col] <= 0:
                    del pending[col]
                col += 1
                continue
            if ci >= len(cells):
                break
            text, rs, cs = cells[ci]
            ci += 1
            for k in range(cs):
                row.append(text if (k == 0 or ri == 0) else "")
                if rs > 1:
                    pending[col] = rs - 1
                col += 1
        grid.append(row)
    width = max(len(r) for r in grid)
    grid = [r + [""] * (width - len(r)) for r in grid]
    return grid


def _split_empty_columns(rows):
    """Split a table on columns that are empty in every row.

    Side-by-side tables in the PDF (two year groups, two palaces) parse
    as one table with a spacer column; splitting keeps each sub-table
    intact instead of merging unrelated column groups.
    """
    if not rows:
        return []
    width = len(rows[0])
    empty = [all(not r[c] for r in rows) for c in range(width)]
    groups, cur = [], []
    for c in range(width):
        if empty[c]:
            if cur:
                groups.append(cur)
                cur = []
        else:
            cur.append(c)
    if cur:
        groups.append(cur)
    if len(groups) <= 1:
        return [rows]
    return [[ [r[c] for c in g] for r in rows] for g in groups]


def _has_letter(cell):
    return bool(re.search(r"[A-Za-z가-힣]", cell))


def _valid_table(rows):
    """Strict gate: header + >=2 data rows, textual headers, labelled rows.

    Rejects bar-chart axis junk (numeric/empty headers, single-value
    rows) and fragmented parses (inconsistent widths, mostly empty).
    """
    if len(rows) < 3:
        return False
    width = len(rows[0])
    if width < 2:
        return False
    if any(len(r) != width for r in rows):
        return False
    header = rows[0]
    nonempty_h = [c for c in header if c]
    if len(nonempty_h) < 2:
        return False
    if sum(1 for c in nonempty_h if _has_letter(c)) * 2 < len(nonempty_h):
        return False
    data = rows[1:]
    if len(data) < 2:
        return False
    for r in data:
        if sum(1 for c in r if c) < 2:
            return False
    # First column must carry labels (words or years), not axis ticks
    # mixed with blanks: at least half the data rows need a non-empty
    # first cell, and the table must not be mostly empty overall.
    first_filled = sum(1 for r in data if r[0])
    if first_filled * 2 < len(data):
        return False
    total = sum(len(r) for r in data)
    filled = sum(1 for r in data for c in r if c)
    if filled * 2 < total:
        return False
    return True


def _flatten_header(rows):
    """Merge a multi-row header (rowspan/colspan source) per column.

    Header rows are the leading rows whose cells are all either empty or
    shared with the row below (a rowspan stub). In practice the kordoc
    HTML tables have at most two header rows; joining the non-empty
    cells per column reproduces the source wording without inventing
    labels. Pipe tables have a single header row and pass through.
    """
    if len(rows) < 2:
        return rows
    # Detect a second header row: it holds short labels (years, units)
    # while data rows below hold values. Only merge when row 1 looks
    # like a header continuation: every non-empty cell is short and the
    # row below it (row 2) starts a label column.
    if len(rows) >= 3:
        second = rows[1]
        third = rows[2]
        second_short = all(len(c) <= 24 for c in second if c)
        # A data row's first cell is a label and later cells are values;
        # a header-continuation row has an empty first cell (rowspan stub).
        if not second[0] and second_short and third[0]:
            merged = []
            for c in range(len(rows[0])):
                parts = [rows[0][c], second[c]]
                merged.append(" ".join(p for p in parts if p).strip())
            return [merged] + rows[2:]
    return rows


def _format_table(rows):
    rows = _flatten_header(rows)
    out = ["| " + " | ".join(rows[0]) + " |",
           "| " + " | ".join("---" for _ in rows[0]) + " |"]
    for r in rows[1:]:
        out.append("| " + " | ".join(r) + " |")
    return "\n".join(out)


def _title_lines(region_lines, table_start):
    """English title lines between the item header and the first table.

    The first title line must start uppercase/digit (a lowercase start
    is a clipped fragment). At most one continuation line follows, and
    only when it starts lowercase ('by Region in ...') or is a
    parenthetical unit line. Column-group label lines ('Changgyeonggung
    Palace Deoksugung Palace') are not titles and stop the capture.
    """
    cands = []
    for ln in region_lines[1:table_start]:
        s = ln.strip()
        if not s or s.startswith(_TITLE_SKIP):
            continue
        if re.search(r"[가-힣]", s):
            continue
        if "<u>" in s or "</u>" in s:
            continue
        if not cands:
            if s[0].islower():
                continue
            if re.fullmatch(r"\(?[%()\d\s.,\-–—:;]+\)?", s):
                continue
            cands.append(s)
        else:
            if s[0].islower() or s.startswith("("):
                cands.append(s)
                if len(cands) >= 3:
                    break
            else:
                break
    return cands[:3]


def _note_lines(region_lines, table_end):
    """Note/footnote lines after the last table block (at most 2)."""
    notes = []
    for ln in region_lines[table_end:]:
        s = ln.strip()
        if not s:
            continue
        if s.startswith("Note:") or s.startswith("※") or s.startswith("*"):
            notes.append(s if s.startswith("*") else s)
            if len(notes) >= 2:
                break
            continue
        if re.match(r"^The\s+(tables?\s+above|above\s+)", s):
            break
        if HDR_RE.match(s):
            break
        # Option prose or anything else: stop at the first non-note line
        # once we already passed the table (notes sit directly below it).
        if notes:
            break
    return notes


def extract_graph_blocks(problem_md_text):
    """Return {qnum: block_text} for Q25 items with a valid data table.

    block_text is the title line(s), the markdown table(s), and any
    note lines, in source order. Items whose tables fail validation get
    no entry (caller logs + queues them). Only qnum 25 is considered:
    other items' tables belong to the option-table path.
    """
    regions = _regions(problem_md_text)
    out = {}
    if 25 not in regions:
        return out
    lines = regions[25]
    # Collect table blocks: pipe blocks and <table> blocks, in order.
    blocks = []  # (start_idx, end_idx, rows)
    for s, e, rows in _parse_pipe_blocks(lines):
        blocks.append((s, e, rows))
    joined = "\n".join(lines)
    for m in re.finditer(r"<table[^>]*>(.*?)</table>", joined, flags=re.S | re.I):
        rows = _parse_html_table(m.group(0))
        if rows is None:
            continue
        start_line = joined[:m.start()].count("\n")
        end_line = joined[:m.end()].count("\n") + 1
        blocks.append((start_line, end_line, rows))
    blocks.sort()
    if not blocks:
        return out
    first_start = blocks[0][0]
    last_end = blocks[-1][1]
    tables = []
    for _s, _e, rows in blocks:
        for sub in _split_empty_columns(rows):
            if _valid_table(sub):
                tables.append(sub)
    if not tables:
        return out
    parts = []
    title = " ".join(_title_lines(lines, first_start))
    if title:
        parts.append(title)
    for sub in tables:
        parts.append(_format_table(sub))
    for nt in _note_lines(lines, last_end):
        parts.append(re.sub(r"^※\s*", "", nt))
    out[25] = "\n\n".join(parts)
    return out


def split_stem_prose(passage):
    """Split a flattened Q25 passage into (furniture, stem_prose).

    The stem prose starts at 'The (above) table(s)/graph above shows'
    and runs to the end. Returns ("", "") when no stem marker is found.
    """
    if not passage:
        return "", ""
    m = STEM_MARKER_RE.search(passage)
    if not m:
        return passage, ""
    return passage[:m.start()].strip(), passage[m.start():].strip()
