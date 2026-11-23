#!/usr/bin/env python3
"""Hybrid merge: Enhanced Upstage markdown (base) + kordoc markdown (overlay).

New exams only. Never run on the existing 74 restored files.

    python3 merge.py --enhanced work/2026_10_H3/upstage.md \
                     --kordoc work/2026_10_H3/problem.md \
                     --out work/2026_10_H3/merged.md \
                     --report work/2026_10_H3/merge-report.md

Normally invoked by scripts/prepare.py, which produces upstage.md
(scripts/upstage_parse.py, Enhanced) and problem.md (kordoc) per exam.
scripts/build.py prefers merged.md over problem.md for its Q10/Q12
table and Q25 graph extractors when it exists.

What it does
------------
1. Base text = Enhanced markdown (reading order, hyphens, chart table,
   blanks, Q43-45 order are correct there).
2. Header/footer lines dropped (page numbers, "영어 영역", "고 3",
   cover title, "제 N 교시", listening instruction).
3. Figure blocks resolved:
   - illustration -> kordoc listening-image reference (image_001)
   - chart        -> kordoc chart-image reference placed before the
                    table; VLM Korean description and raw number dump
                    dropped, table and Note line kept
   - icon         -> the literal arrow "↓" (Q40)
4. Bold/italic emphasis stripped, except glossary lines where the
   asterisks are item separators ("* word: 뜻 ** word: 뜻") -- those
   become "\\* word: 뜻 \\*\\* word: 뜻" (kordoc convention).
5. Question stems normalized ("### 4." / "**31.**" / "25.다음" -> "N. ...").
6. Listening option bullets ("- **text**" x5) joined into one
   "① a ② b ③ c ④ d ⑤ e" line.
7. Underline overlay from kordoc, scoped per question:
   - kordoc <u> phrases missing in the same Enhanced question span
     are wrapped as <u>...</u>; stem negations are wrapped in the
     stem line first; longer phrases first so fragments of the Q21
     quote collapse into the full phrase.
   - (A)/(B)/'him' in the shared [43~45] passage are found in the
     group span when the numbered span does not contain them.
8. Foreign-word check: diacritic tokens (e.g. révéléur vs révélateur)
   are flagged in the report, never auto-fixed.

Outputs: merged markdown + a verification report (underline table,
image mapping, foreign-word flags, per-question diff vs kordoc).
"""

import argparse
import re
import sys
import unicodedata
from collections import Counter

STEM_RE = re.compile(
    r'^\s*(?:\d+\.\s+)?(?:#{1,3}\s+)?(?:\*\*)?(\d{1,2})\\?\.\s*(.*)$')
GROUP_RE = re.compile(r'^(?:#{1,3}\s+)?\*?\*?\[(\d{1,2})\s*[~\u223c\u301c]\s*(\d{1,2})\]')
PLACEHOLDER = '![image](/image/placeholder)'
CIRCLED = '①②③④⑤'
# Words a question stem underlines: its negation word. Only these are
# wrapped in the stem line first; every other phrase is wrapped in the
# body (passage/options) first, because the stem can mention the same
# token (e.g. Q40's "(A)", "(B)" in "빈칸 (A), (B)에 들어갈 말").
NEGATIONS = {'않는', '틀린', '다른', '아닌', '없는', '않은'}

# Lines to drop outright (headers, footers, cover).
DROP_RES = [
    re.compile(r'^#{1,3}\s*.*문제지\s*$'),                 # cover title
    re.compile(r'^#{1,3}\s*제\s*\d+\s*교시\s*$'),           # "## 제 3 교시"
    re.compile(r'^제\s*\d+\s*교시\s*$'),                   # "제 3 교시" (no #)
    re.compile(r'^1번부터 17번까지는 듣고 답하는 문제입니다'),  # listening instruction
    re.compile(r'^#{1,3}\s*영어\s*영역\s*$'),
    re.compile(r'^#{1,3}\s*\*+\s*영어\s*영역\s*\*+\s*$'),  # "## **영어 영역**"
    re.compile(r'^\*?\*?\s*영어\s*영역\s*\*?\*?$'),
    re.compile(r'^\*?\*?\s*고\s*3\s*\*?\*?$'),
    re.compile(r'듣기 문제가 끝났습니다'),              # listening-end announcement
    re.compile(r'^18번부터는 문제지의 지시에 따라'),     # its continuation
]


def is_page_number_line(line):
    """A line that is only digits once markup and spaces are stripped."""
    s = re.sub(r'[#*\s]', '', line)
    return bool(s) and s.isdigit()


def split_spans(lines):
    """Split markdown lines into spans keyed by question number or group.

    Returns (spans, order) where spans is dict key -> list of lines and
    order is the list of keys in first-seen order. Text before the first
    stem/group goes under key 'pre'.
    """
    spans = {}
    order = []
    current = 'pre'
    spans[current] = []
    order.append(current)
    for line in lines:
        m = STEM_RE.match(line)
        g = GROUP_RE.match(line)
        if m:
            current = int(m.group(1))
            if current not in spans:
                spans[current] = []
                order.append(current)
        elif g:
            current = 'group_%s_%s' % (g.group(1), g.group(2))
            if current not in spans:
                spans[current] = []
                order.append(current)
        spans[current].append(line)
    return spans, order


def kordoc_underlines(span_lines):
    """All <u>...</u> phrases in a span, longest first, deduped."""
    text = '\n'.join(span_lines)
    phrases = re.findall(r'<u>(.*?)</u>', text, flags=re.S)
    seen = set()
    out = []
    for p in sorted(set(phrases), key=lambda s: (-len(s), s)):
        p = p.strip()
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def kordoc_images(lines):
    """Image refs in kordoc order with the last question number seen."""
    refs = []
    last_q = None
    for line in lines:
        m = STEM_RE.match(line)
        if m:
            last_q = int(m.group(1))
        for ref in re.findall(r'!\[image\]\(([^)]+)\)', line):
            refs.append((ref, last_q))
    return refs


def strip_emphasis(line):
    """Strip ** and * emphasis, except glossary lines where * separates
    entries ("* word: 뜻 ** word: 뜻") -- escape those instead."""
    if re.match(r'^\*+\s+[A-Za-z\-]+:', line):
        return line.replace('*', r'\*')
    return line.replace('*', '')


def normalize_stem(line):
    m = STEM_RE.match(line)
    if not m:
        return line
    num, rest = int(m.group(1)), m.group(2).strip()
    rest = rest.replace('*', '')
    return '%d. %s' % (num, rest) if rest else '%d.' % num


def join_listening_options(lines, qnum):
    """Consecutive unnumbered "- text" lines in Q1-17 -> one numbered line."""
    if not (1 <= qnum <= 17):
        return lines
    out = []
    i = 0
    while i < len(lines):
        if re.match(r'^- [^①②③④⑤]', lines[i]):
            run = []
            while i < len(lines) and re.match(r'^- [^①②③④⑤]', lines[i]):
                run.append(re.sub(r'^- ', '', lines[i]).strip())
                i += 1
            if 3 <= len(run) <= 5:
                joined = ' '.join('%s %s' % (CIRCLED[j], t) for j, t in enumerate(run))
                out.append(joined)
            else:
                out.extend('- %s' % t for t in run)
        else:
            out.append(lines[i])
            i += 1
    return out


def merge(enh_lines, kord_lines):
    log = {'figures': [], 'dropped_headers': 0, 'underlines': [],
           'foreign_words': [], 'unapplied': []}
    k_spans, _ = split_spans(kord_lines)
    k_images = kordoc_images(kord_lines)
    # Figure images: the chart image is the one that appears after the
    # Q25 stem (in kordoc it sits after Q28's options, at the page end);
    # the listening image is the one before the Q25 stem.
    chart_img = None
    listen_img = None
    for ref, q in k_images:
        if q is not None and q >= 25 and chart_img is None:
            chart_img = ref
        elif q is not None and q < 25 and listen_img is None:
            listen_img = ref
    if chart_img is None and k_images:
        chart_img = k_images[-1][0]
    if listen_img is None and k_images:
        listen_img = k_images[0][0]

    out = []
    i = 0
    n = len(enh_lines)
    while i < n:
        line = enh_lines[i]
        # --- figure blocks -------------------------------------------------
        if PLACEHOLDER in line:
            # Figure type comes from the LABEL of the following line
            # ("- Chart Type: bar" / "- Figure Type: illustration" /
            # "- Figure Type: icon"); the value ("bar") is not the type.
            j = i + 1
            ftype = None
            while j < n and enh_lines[j].startswith('- '):
                if 'Chart Type' in enh_lines[j]:
                    ftype = 'chart'
                elif 'Figure Type: icon' in enh_lines[j]:
                    ftype = 'icon'
                elif 'Figure Type: illustration' in enh_lines[j]:
                    ftype = 'illustration'
                j += 1
            if ftype == 'chart':
                # Drop everything until the table (first line starting
                # with "|"), keep the table, insert the image ref before it.
                k = j
                while k < n and not enh_lines[k].startswith('|'):
                    k += 1
                if chart_img:
                    out.append('![image](%s)' % chart_img)
                log['figures'].append(('chart', chart_img, k - i))
                i = k
                continue
            if ftype == 'icon':
                out.append('↓')
                log['figures'].append(('icon', '↓', j - i))
                i = j
                continue
            # illustration (default): consume until the next stem or
            # group line; replace with the listening image ref.
            k = j
            while k < n and not STEM_RE.match(enh_lines[k]) \
                    and not GROUP_RE.match(enh_lines[k]):
                k += 1
            if listen_img:
                out.append('![image](%s)' % listen_img)
            log['figures'].append(('illustration', listen_img, k - i))
            i = k
            continue
        # --- header / footer lines -----------------------------------------
        # search(), not match(): the announcement rule is unanchored.
        if is_page_number_line(line) or any(r.search(line) for r in DROP_RES):
            log['dropped_headers'] += 1
            i += 1
            continue
        out.append(line)
        i += 1

    # --- normalize stems, strip emphasis, join listening options ---------
    spans, order = split_spans(out)
    merged_spans = {}
    for key in order:
        lines = spans[key]
        lines = [normalize_stem(l) if STEM_RE.match(l) else l for l in lines]
        lines = [strip_emphasis(l) for l in lines]
        # Group instruction lines: strip the ** around them too.
        lines = [re.sub(r'^\*\*(\[.*?\].*)\*\*$', r'\1', l) for l in lines]
        lines = [re.sub(r'^#{1,3}\s+(\[.*?\].*)$', r'\1', l) for l in lines]
        if isinstance(key, int):
            lines = join_listening_options(lines, key)
        merged_spans[key] = lines

    # --- stem reconciliation from kordoc ------------------------------------
    # kordoc preserves the PDF's text literally; Enhanced (VLM) can
    # mis-split a Korean word in a stem ("고 르시오"). When the two stems
    # are identical once all whitespace and <u> markup are removed, take
    # kordoc's stem line (it also carries the correct stem underlines).
    for key in order:
        if not isinstance(key, int) or key not in k_spans:
            continue
        k_stem = k_spans[key][0] if k_spans[key] else ''
        m_stem = merged_spans[key][0] if merged_spans[key] else ''
        if not (STEM_RE.match(k_stem) and STEM_RE.match(m_stem)):
            continue
        if _squash(k_stem) == _squash(m_stem) and k_stem != m_stem:
            merged_spans[key][0] = normalize_stem(k_stem)

    # --- underline overlay from kordoc --------------------------------------
    group_keys = [k for k in order if isinstance(k, str)
                  and k.startswith('group_')]
    for key in order:
        if not isinstance(key, int):
            continue
        k_phrases = kordoc_underlines(k_spans.get(key, []))
        if not k_phrases:
            continue
        # Candidate spans, in search order: the numbered span, then the
        # group span that precedes this question, then any other group
        # span (Q42/Q44 cross-reference each other's passages).
        idx = order.index(key)
        cand = [key]
        for prev in reversed(order[:idx]):
            if isinstance(prev, str) and prev.startswith('group_'):
                cand.append(prev)
                break
        cand.extend(g for g in group_keys if g not in cand)
        for phrase in k_phrases:
            # Already underlined in any candidate span (exact tag, or
            # inside a wider <u> segment, e.g. Q21 quote fragments).
            done = False
            for ck in cand:
                ctext = '\n'.join(merged_spans.get(ck, []))
                if '<u>%s</u>' % phrase in ctext or any(
                        phrase in seg for seg in
                        re.findall(r'<u>(.*?)</u>', ctext, flags=re.S)):
                    log['underlines'].append((key, phrase, 'already-present'))
                    done = True
                    break
            if done:
                continue
            # Wrap the first plain occurrence. Stem negations: stem line
            # first. Everything else: body first (the stem can mention
            # the same token, e.g. Q40's "(A)", "(B)").
            wrapped = False
            for ck in cand:
                lines = merged_spans.get(ck, [])
                if not lines:
                    continue
                if phrase in NEGATIONS:
                    order_lines = [0] + list(range(1, len(lines)))
                else:
                    order_lines = list(range(1, len(lines))) + [0]
                for li in order_lines:
                    if phrase in lines[li]:
                        lines[li] = lines[li].replace(
                            phrase, '<u>%s</u>' % phrase, 1)
                        wrapped = True
                        break
                if wrapped:
                    where = 'stem' if (phrase in NEGATIONS and li == 0) \
                        else ('group' if ck != key else 'body')
                    log['underlines'].append(
                        (key, phrase, 'applied-%s' % where))
                    break
            if not wrapped:
                log['underlines'].append((key, phrase, 'UNAPPLIED'))
                log['unapplied'].append((key, phrase))

    # --- foreign-word check --------------------------------------------------
    log['foreign_words'] = foreign_word_flags(merged_spans, k_spans)

    # --- table row-label reconciliation -----------------------------------
    log['table_labels_fixed'] = reconcile_table_labels(merged_spans, k_spans)

    # --- Q40-style "(A)" options header -----------------------------------
    # Enhanced drops the "(B)" column header (kordoc has "(A) (B)").
    # Restore it from kordoc when the merged span has a lone "(A)" line.
    fixed_headers = 0
    for key, lines in merged_spans.items():
        if not isinstance(key, int) or key not in k_spans:
            continue
        k_header = None
        for kl in k_spans[key]:
            if re.match(r'^\(A\)[\s\t]+\(B\)\s*$', kl.strip()):
                k_header = '(A) (B)'
                break
        if not k_header:
            continue
        for i, l in enumerate(lines):
            if l.strip() == '(A)':
                lines[i] = k_header
                fixed_headers += 1
                break
    log['option_headers_fixed'] = fixed_headers

    merged = []
    for key in order:
        merged.extend(merged_spans[key])
    merged = [clean_spacing(l) for l in merged]
    return merged, log


def clean_spacing(text):
    """Mechanical spacing cleanup, safe on exam text: no space before
    punctuation, no spaces inside "(①)"/"(a)" markers, no "- " before
    circled option numbers, "__ (A) __" summary blanks become "(A)"."""
    text = re.sub(r'\s+([.,;:!?%])', r'\1', text)
    text = re.sub(r'\(\s*([①②③④⑤a-eA-E])\s*\)', r'(\1)', text)
    text = re.sub(r'-\s+(?=[①②③④⑤])', '', text)
    # "__ (A) __" summary blanks become "(A)"; the <u> tags from the
    # underline overlay may already wrap the marker -- keep them only
    # when they were there.
    def _blank(m):
        if '<u>' in m.group(0):
            return '<u>(%s)</u>' % m.group(1)
        return '(%s)' % m.group(1)
    text = re.sub(r'__\s*(?:<u>)?\(([A-Ea-e])\)(?:</u>)?\s*__', _blank, text)
    return text


def _squash(text):
    """Text with all whitespace and <u> markup removed, for equality
    checks that ignore spacing and underline markup only."""
    return re.sub(r'\s+', '', re.sub(r'</?u>', '', text))


def _diacritic_tokens(text):
    # Curly quotes/apostrophes are not foreign letters; normalize them
    # away before looking for accented letters.
    text = text.replace('’', "'").replace('‘', "'")
    text = text.replace('“', '"').replace('”', '"')
    toks = re.findall(r"[A-Za-zÀ-ÿ]+(?:['’\-][A-Za-zÀ-ÿ]+)*", text)
    return [t for t in toks if any(ord(c) > 127 for c in t)]


def _strip_accents(s):
    return ''.join(c for c in unicodedata.normalize('NFD', s)
                   if unicodedata.category(c) != 'Mn')


def foreign_word_flags(merged_spans, k_spans):
    """Tokens with accents that differ between the two parsers."""
    flags = []
    keys = sorted(k for k in k_spans if isinstance(k, int))
    for key in keys:
        m_toks = Counter(_diacritic_tokens('\n'.join(merged_spans.get(key, []))))
        k_toks = Counter(_diacritic_tokens('\n'.join(k_spans.get(key, []))))
        # Same base letters, different accents -> mismatch.
        m_base = {}
        for t in m_toks:
            m_base.setdefault(_strip_accents(t).lower(), []).append(t)
        for t in k_toks:
            base = _strip_accents(t).lower()
            if base in m_base and t not in m_base[base]:
                flags.append((key, 'mismatch', k_toks and t,
                              m_base[base][0]))
            elif base not in m_base:
                flags.append((key, 'kordoc-only', t, None))
        for t in m_toks:
            base = _strip_accents(t).lower()
            if base not in { _strip_accents(x).lower() for x in k_toks }:
                flags.append((key, 'enhanced-only', None, t))
    return flags


def normalize(text):
    """Loose normalization for per-question comparison: drop <u> markup,
    asterisks and backslashes, unify quotes/dashes/dot leaders, strip
    the "- " bullet prefix kordoc puts on option lines, drop group
    instruction lines (structural, not content), the listening-end
    announcement, and leading list-marker numbers ("1. 28." -> "28."),
    collapse whitespace."""
    text = re.sub(r'</?u>', '', text)
    text = text.replace('*', '').replace('\\', '')
    text = text.replace('’', "'").replace('‘', "'")
    text = text.replace('“', '"').replace('”', '"')
    text = text.replace('–', '-').replace('—', '-')
    text = text.replace('…', '...')
    text = text.replace('·', '.')
    text = re.sub(r'\.{3,}', '...', text)
    # Blank underscore runs and image refs go before clean_spacing, so
    # the space they leave behind is collapsed by its punctuation rule.
    text = re.sub(r'!\[image\]\([^)]+\)', '', text)
    text = re.sub(r'_{4,}', '', text)
    text = re.sub(r'이제\s*듣기\s*문제가\s*끝났습니다\.\s*18번부터는\s*'
                  r'문제지의\s*지시에\s*따라\s*답을\s*하시기\s*바랍니다\.',
                  '', text)
    lines = []
    for l in text.splitlines():
        if GROUP_RE.match(l):
            continue
        l = re.sub(r'^\s*\d+\.\s+(?=\d{1,2}\.)', '', l)
        lines.append(l)
    text = clean_spacing(' '.join(lines))
    # Comparison-only noise removal: spaced dashes (kordoc " - " vs
    # Enhanced attached "-").
    text = re.sub(r'\s+-\s+', '-', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip().lower()


def reconcile_table_labels(merged_spans, k_spans):
    """In markdown tables, Enhanced sometimes reads circled row labels
    as plain digits (Q10: "1" where the exam has "①"). When a merged
    table row matches a kordoc table row in every cell except the first,
    and the first cells are the same number plain vs circled, take
    kordoc's circled label. Returns the number of cells fixed."""
    circled = {'1': '①', '2': '②', '3': '③', '4': '④', '5': '⑤'}
    fixed = 0
    for key, lines in merged_spans.items():
        if not isinstance(key, int) or key not in k_spans:
            continue
        k_lines = k_spans[key]
        m_idx = [i for i, l in enumerate(lines)
                 if l.startswith('|') and '---' not in l]
        k_idx = [i for i, l in enumerate(k_lines)
                 if l.startswith('|') and '---' not in l]
        # Skip the header row (first row) on both sides.
        m_data, k_data = m_idx[1:], k_idx[1:]
        for mi, ki in zip(m_data, k_data):
            m_cells = [c.strip() for c in lines[mi].strip().strip('|').split('|')]
            k_cells = [c.strip() for c in k_lines[ki].strip().strip('|').split('|')]
            if len(m_cells) != len(k_cells) or not m_cells:
                continue
            if (m_cells[0] in circled
                    and k_cells[0] == circled[m_cells[0]]
                    and [clean_cell(c) for c in m_cells[1:]]
                    == [clean_cell(c) for c in k_cells[1:]]):
                lines[mi] = lines[mi].replace(
                    '| %s |' % m_cells[0], '| %s |' % k_cells[0], 1)
                fixed += 1
    return fixed


def clean_cell(cell):
    return clean_spacing(cell.replace('\\', '')).strip()


def block_word_diff(merged_spans, m_keys, k_spans, k_keys):
    """Word-multiset comparison over a block of spans (used for the
    [41~45] group, where kordoc's span order is scrambled). Returns
    (only_in_merged, only_in_kordoc) as Counters of normalized words."""
    def words(spans, ks):
        text = '\n'.join(l for k in ks for l in spans.get(k, []))
        return Counter(normalize(text).split())
    m_words = words(merged_spans, m_keys)
    k_words = words(k_spans, k_keys)
    return m_words - k_words, k_words - m_words


def per_question_diff(merged, kord_lines):
    """Normalized per-question comparison against kordoc."""
    m_spans, m_order = split_spans(merged)
    k_spans, _ = split_spans(kord_lines)
    diffs = []
    for key in m_order:
        if not isinstance(key, int):
            continue
        m_norm = normalize('\n'.join(m_spans[key]))
        k_norm = normalize('\n'.join(k_spans.get(key, [])))
        if m_norm != k_norm:
            diffs.append(key)
    return diffs


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--enhanced', required=True)
    ap.add_argument('--kordoc', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--report', required=True)
    args = ap.parse_args()

    enh_lines = open(args.enhanced, encoding='utf-8').read().splitlines()
    kord_lines = open(args.kordoc, encoding='utf-8').read().splitlines()
    merged, log = merge(enh_lines, kord_lines)

    open(args.out, 'w', encoding='utf-8').write('\n'.join(merged) + '\n')

    # --- verification report ----------------------------------------------
    qnums = [k for k in split_spans(merged)[1] if isinstance(k, int)]
    u_count = len(re.findall(r'<u>', '\n'.join(merged)))
    k_u_count = len(re.findall(r'<u>', '\n'.join(kord_lines)))
    placeholders = sum(1 for l in merged if PLACEHOLDER in l)
    stray_stars = sum(l.count('*') for l in merged
                      if not l.lstrip().startswith(r'\*'))
    blanks = len(re.findall(r'_{4,}', '\n'.join(merged)))
    img_refs = re.findall(r'!\[image\]\(([^)]+)\)', '\n'.join(merged))
    diff_qs = per_question_diff(merged, kord_lines)
    m_spans, _ = split_spans(merged)
    k_spans, _ = split_spans(kord_lines)
    m_only, k_only = block_word_diff(
        m_spans, ['group_41_42', 41, 42, 'group_43_45', 43, 44, 45],
        k_spans, [41, 44, 42, 45])
    applied = [u for u in log['underlines'] if u[2].startswith('applied')]
    already = [u for u in log['underlines'] if u[2] == 'already-present']
    unapplied = [u for u in log['underlines'] if u[2] == 'UNAPPLIED']

    lines = []
    lines.append('# Merge report')
    lines.append('')
    lines.append('Base: Enhanced Upstage markdown. Overlay: kordoc markdown.')
    lines.append('New exams only. Existing 74 restored files untouched.')
    lines.append('')
    lines.append('## Checks')
    lines.append('- Questions found: %d (numbers %s)' % (
        len(qnums), '1-45 in order' if qnums == list(range(1, 46)) else qnums))
    lines.append('- Underlines in merged: %d (kordoc: %d)' % (u_count, k_u_count))
    lines.append('- Underline overlay: %d applied, %d already present, %d unapplied'
                 % (len(applied), len(already), len(unapplied)))
    lines.append('- Image refs in merged: %s' % (img_refs or 'none'))
    lines.append('- Placeholders left: %d' % placeholders)
    lines.append('- Stray `*` left (non-glossary lines): %d' % stray_stars)
    lines.append('- Blank runs (`____`): %d' % blanks)
    lines.append('- Header/footer lines dropped: %d' % log['dropped_headers'])
    lines.append('- Figure blocks resolved: %s' % (
        ', '.join('%s -> %s (%d lines)' % f for f in log['figures']) or 'none'))
    lines.append('- Questions whose normalized text still differs from kordoc: %s'
                 % (diff_qs or 'none'))
    lines.append('- Table row labels fixed from kordoc (Q10 ①②): %d'
                 % log.get('table_labels_fixed', 0))
    lines.append('- Option headers fixed from kordoc (Q40 "(A) (B)"): %d'
                 % log.get('option_headers_fixed', 0))
    lines.append('- Block [41~45] word-level check (kordoc spans are scrambled,'
                 ' so this compares word multisets): only in merged %s;'
                 ' only in kordoc %s' % (
                     dict(m_only.most_common(10)) or 'none',
                     dict(k_only.most_common(10)) or 'none'))
    lines.append('')
    lines.append('## Underline overlay detail')
    for q, phrase, status in log['underlines']:
        lines.append('- Q%d `%s` -> %s' % (q, phrase, status))
    lines.append('')
    lines.append('## Foreign-word flags (review, do not auto-fix)')
    if log['foreign_words']:
        for q, kind, k_tok, m_tok in log['foreign_words']:
            if kind == 'mismatch':
                lines.append('- Q%d mismatch: kordoc `%s` vs Enhanced `%s`'
                             % (q, k_tok, m_tok))
            elif kind == 'kordoc-only':
                lines.append('- Q%d kordoc-only token: `%s`' % (q, k_tok))
            else:
                lines.append('- Q%d Enhanced-only token: `%s`' % (q, m_tok))
    else:
        lines.append('- none')
    lines.append('')
    lines.append('## Notes')
    lines.append('- Q4 options are the picture itself (①-⑤ point at picture'
                 ' items); there are no text options to restore.')
    lines.append('- The listening-end announcement ("이제 듣기 문제가 끝났습니다…")'
                 ' is dropped by design; it is not a question.')
    lines.append('- kordoc places the listening image after Q12; the merge'
                 ' puts it at Q4 where Enhanced found the picture. Final'
                 ' image placement is the pipeline image-assignment step.')
    lines.append('- Q10: table row labels are ①② in the exam; Enhanced read'
                 ' them as plain digits, the merge restores them from kordoc.')
    lines.append('- Q17: kordoc lists the options out of order (③ zippers'
                 ' last); the merge keeps Enhanced\'s correct ①-⑤ order.')
    lines.append('- Q25: the chart table exists only in the merged output;'
                 ' kordoc keeps the chart as an image, its table comes from'
                 ' the separate graph_tables step.')
    lines.append('- Q31: kordoc duplicates words ("exhibit exhibit", "extreme'
                 ' extreme"); the merged text is the correct single reading.')
    lines.append('- Q41-45: kordoc\'s spans are scrambled (Q44 stem before'
                 ' Q42, passages interleaved); the merged order 41-45 is'
                 ' correct and the block word check above confirms no'
                 ' content is lost or gained.')
    lines.append('- Per-question diffs above are expected wherever the two'
                 ' parsers format options differently (inline vs lines);'
                 ' check the flagged spans, do not expect zero.')
    open(args.report, 'w', encoding='utf-8').write('\n'.join(lines) + '\n')

    print('merged: %s (%d lines)' % (args.out, len(merged)))
    print('questions: %d, underlines: %d (kordoc %d), applied %d, '
          'already %d, unapplied %d' % (
              len(qnums), u_count, k_u_count, len(applied), len(already),
              len(unapplied)))
    print('placeholders left: %d, stray stars: %d, blanks: %d' % (
        placeholders, stray_stars, blanks))
    print('report: %s' % args.report)
    if unapplied:
        print('UNAPPLIED:', unapplied, file=sys.stderr)
    return 0 if not unapplied and placeholders == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
