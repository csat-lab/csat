#!/usr/bin/env python3
"""csat restoration pipeline (repository port, phase 1).

Inputs (repository layout):
- data/raw/<YYYY>년_<M>월_고<N>_수능영어_문제.pdf and _정답.pdf
- Prepared parses in the work directory (see scripts/prepare.py):
  work/<stem>/problem.md, answer.md, problem_cols.txt, answer_cols.txt,
  answer_full.txt, images/problem/

Output: data/output/고<grade>/<YYYY>_M<month>_H<grade>_restored.md plus
images as JPEG, or a temporary directory in --dry-run mode.

Clean format only: number, question prompt, restored passage. 1-17 options
kept (no dash before circled numbers). 18-45 options removed, circled
numbers and parenthesis segment labels removed from passages. No notes,
no appendix in output. Uncertain cases are logged to the work directory
restoration_log.json, NOT into outputs.

Safety: without --force, an existing output file is skipped, never
overwritten. --dry-run never writes to the output directory.

This is a faithful port of the workspace pipeline. Format decisions made
after it was written (Q40 summary removal, Q36/Q37 joining) are NOT yet
reflected here; they are phase 2 work.
"""
import os, re, json, glob, subprocess, sys, tempfile
from pathlib import Path

STEM_RE = re.compile(r"^(\d{4})년_(\d{1,2})월_고([123])_수능영어_문제\.pdf$")


def default_repo_root():
    """Repository root: parent of the directory holding this script."""
    return Path(__file__).resolve().parent.parent


def stem_from_filename(name):
    """Korean raw filename -> stem like 2026_03_H3, or None if no match."""
    m = STEM_RE.match(name)
    if not m:
        return None
    year, month, grade = m.group(1), int(m.group(2)), m.group(3)
    return f"{year}_{month:02d}_H{grade}"


def discover_stems(raw_dir):
    """Map stem -> problem PDF path for every Korean-named problem PDF."""
    found = {}
    for p in sorted(raw_dir.glob("*_수능영어_문제.pdf")):
        stem = stem_from_filename(p.name)
        if stem is not None:
            found[stem] = p
    return found
CIRC = "①②③④⑤"
CIRC_MAP = {c: i+1 for i, c in enumerate(CIRC)}
# Dingbat circled digits -> ①-⑤ (see clean_lines).
_DINGBAT_FOLD = str.maketrans(
    "➀➁➂➃➄❶❷❸❹❺⓵⓶⓷⓸⓹", "①②③④⑤①②③④⑤①②③④⑤")
HYPHEN_FIX = {
    "onemonth": "one-month", "oneonone": "one-on-one", "professionalgrade": "professional-grade",
    "characterbranded": "character-branded", "considerationgeneratorsquelchers": "consideration-generator-squelchers",
    "imagisticcategorial": "imagistic-categorial", "AIdriven": "AI-driven", "problemsolving": "problem-solving",
    "everevolving": "ever-evolving", "selfview": "self-view", "selfperception": "self-perception",
    "7yearold": "7-year-old", "7year": "7-year", "coexpressive": "co-expressive",
    "quasiuniversal": "quasi-universal", "neverending": "never-ending", "nonexistent": "non-existent",
    "wellrounded": "well-rounded", "thirdage": "third-age", "selfworth": "self-worth",
}
def fix_hyphens(t):
    t = t.replace("\u00ad", "-").replace("­", "-")
    for k, v in HYPHEN_FIX.items():
        t = t.replace(k, v)
    return t

def cols_text(src):
    """Left column then right column per page, A3 841x1190 assumed; fallback to full layout."""
    try:
        info = subprocess.check_output(["pdfinfo", str(src)], stderr=subprocess.DEVNULL).decode(errors="ignore")
        pages = int([l for l in info.splitlines() if l.startswith("Pages")][0].split(":")[1])
        size = [l for l in info.splitlines() if "Page size" in l]
        w_total = 841
        if size:
            try: w_total = int(float(size[0].split(":")[1].split("x")[0]))
            except Exception: pass
    except Exception:
        return None, 0
    out = []
    mid = w_total // 2
    for p in range(1, pages+1):
        for x, w in [(0, mid+1), (mid, w_total-mid)]:
            try:
                t = subprocess.check_output(["pdftotext","-f",str(p),"-l",str(p),"-x",str(x),"-y","0","-W",str(w),"-H","1190","-layout",str(src),"-"], stderr=subprocess.DEVNULL).decode(errors="ignore")
            except Exception:
                t = ""
            out.append(t)
    return "\n".join(out), pages

def clean_lines(text):
    lines = []
    for raw in text.splitlines():
        # Replace Private Use Area glyphs (e.g. U+F000 markers pdftotext
        # cannot map) with a space, never delete them: a marker can sit
        # between two words ("us<U+F000>independently"), and deleting it
        # glues them ("usindependently"). A lone marker line becomes a
        # blank line, as before.
        raw = re.sub(r"[\ue000-\uf8ff]", " ", raw)
        # Fold dingbat circled digits (➀-➄, ❶-❺, ⓵-⓹) to the ①-⑤ the
        # restored format uses everywhere: no baseline keeps a dingbat
        # form, and option markers otherwise leak the font variant.
        raw = raw.translate(_DINGBAT_FOLD)
        # Fold full-width ASCII punctuation to half-width (＄→$, ＝→=,
        # －→-): font variants from the PDF text layer. Full-width
        # alphanumerics and ～ are left alone.
        raw = "".join(
            chr(ord(c) - 0xFEE0)
            if 0xFF01 <= ord(c) <= 0xFF5E and c != "～" and not (
                0xFF10 <= ord(c) <= 0xFF19
                or 0xFF21 <= ord(c) <= 0xFF3A
                or 0xFF41 <= ord(c) <= 0xFF5A
            )
            else c
            for c in raw
        )
        s = raw.rstrip()
        if not s.strip():
            lines.append("")
            continue
        # A page-header fragment glued to real content ("영역    (B)"):
        # strip leading header tokens while the remainder is non-empty.
        # Only multi-char tokens (영어/영역/고N) or single fragment chars
        # followed by a wide layout gap are stripped, so a Korean word
        # merely starting with 영/고 is never touched.
        while True:
            _fm = re.match(r"^\s*(영어|영역|고[123])(?=\s+)(.*)$", s)
            if _fm and _fm.group(2).strip() and not re.match(r"[가-힣]", _fm.group(2).strip()):
                s = _fm.group(2)
                continue
            _fm = re.match(r"^\s*(영|어|역|고)(?:\s{5,})(.*)$", s)
            if _fm and _fm.group(2).strip() and not re.match(r"[가-힣]", _fm.group(2).strip()):
                s = _fm.group(2)
                continue
            break
        if not s.strip():
            lines.append("")
            continue
        # horizontal separator lines
        if set(s.strip()) <= set("━─═-=") and len(s.strip()) >= 10:
            continue
        # Page-header fragments: lines composed only of header tokens
        # (영어/영역/고N/digits), glued or spaced, e.g. "영어영",
        # "영어 영", "어 영역 고1". The column split cuts the centred
        # header "영어 영역" at varying points, and these lines carry
        # large leading indents, so this must run BEFORE the indent
        # rule below or the fragments become fake blanks.
        if re.fullmatch(r"(영어|영역|고[123]|\d|영|어|역|고)+",
                        s.replace(" ", "").replace("\t", "")):
            continue
        # large leading indent (>=20 spaces) indicates a blank placeholder, except footer/token/footnote/[3점] lines (those are handled below)
        # Exception: a deeply indented line with internal column gaps
        # (2+ spaces) and no Korean is tabular content (a wrapped table
        # header), not a blank: blanking it would destroy the column
        # positions the table fallback parses. Keep it verbatim.
        # NOTE: a 10-space threshold was tried and reverted: mid-passage
        # lines with 14-19 space indents occur both at real blank sites
        # and as layout artifacts (2021_10_H3 "human concerns" carries
        # a 17-space indent but no blank), and text alone cannot tell
        # them apart. Indent-blank sites stay in the fill queue.
        # Speaker labels ("Woman:") are never blanks.
        # NOTE: a wrapped line starting with punctuation is NOT excluded
        # here: 2024_M3_H2 Q32 proves the blank can sit exactly there
        # ("been ____ , particularly..." carries the fill "consumed by
        # audiences of all ages"). Excluding punctuation-start lines
        # loses real blank sites.
        _lead = len(raw) - len(raw.lstrip(" "))
        _is_columnar = bool(re.search(r" {2,}", s.strip())) and not re.search(r"[가-힣]", s.strip())
        _is_speaker = bool(re.fullmatch(r"[A-Za-z][A-Za-z .'-]*:", s.strip()))
        if _lead >= 20 and not _is_columnar and not _is_speaker and not (s.strip() in ("영어","영역","고1","고2","고3") or s.strip().isdigit() or s.strip().startswith("*") or s.strip() == "[3점]"):
            lines.append("____ " + s.strip())
            continue
        # The centred header "전국연합학력평가" can lose its first
        # syllable at the column boundary ("국연합학력평가"); match the
        # tail that always survives.
        if "학력평가" in s:
            continue
        if "문제지" in s:
            continue
        if "확인 사항" in s:
            continue
        if s.strip() in ("했는지 확인하시오.", "◦ 답안지의 해당란에 필요한 내용을 정확히 기입(표기)"):
            continue
        if s.strip().startswith("◦"):
            continue
        if "듣기 문제가 끝났습니다" in s or "문제지의 지시에 따라" in s or "에 따라 답을 하시기 바랍니다" in s:
            continue
        if re.match(r"\s*\*+\s", s) and ":" in s and re.search(r"[가-힣]", s):
            continue
        if "전국연합학력평가 문제지" in s or "제 3 교시" in s or "제3교시" in s:
            continue
        if s.strip().startswith("* 확인 사항") or "답안지의 해당란" in s:
            continue
        if s.strip().startswith("＊") and "확인 사항" in s:
            continue
        if s.strip() == "하시오.":
            continue
        if re.match(r"\s*\*\s", s) and re.search(r"[가-힣]", s):
            # vocabulary footnote line, e.g. "* embed: 파묻다" - remove, as in reference
            continue
        if re.match(r"\s*\*\s*[A-Za-z]", s) and ":" in s and re.search(r"[가-힣]", s):
            # vocabulary footnote without a space after *, e.g. "*gratification: 만족감"
            continue
        lines.append(s)
    return lines

def is_group_heading(line):
    m = re.search(r"\[(\d+)\s*[~～∼-]\s*(\d+)\]\s*(.*)", line)
    return m

def q_header(line):
    # Require space after dot (or end of line for bare headers) to avoid decimal numbers like 42.6% in graphs
    # Accept 0: a clipped "10."/"20."/"30."/"40." header reads as "0." when
    # the leading digit is lost at the column boundary; split_questions
    # repairs it from sequence context and rejects it otherwise.
    # A wide gap (>=10 spaces) between the number and an English
    # remainder is a passage-initial blank (the blank rule renders as
    # spacing, e.g. "31.                     works as ..."): keep it
    # as a "____" marker so the fill handler can find the site.
    m = re.match(r"\s*(\d{1,2})\.(\s*)(.*)$", line)
    if m and 0 <= int(m.group(1)) <= 45:
        gap, rest = m.group(2), m.group(3).strip()
        if not gap and rest:
            return None  # decimal like 42.6%, not a header
        if len(gap) >= 10 and rest and not re.search(r"[가-힣]", rest):
            rest = "____ " + rest
        return int(m.group(1)), rest
    return None

# Circled digits in every block kordoc/pdftotext produce for answer keys.
# Question options use ①-⑤; key tables sometimes render as dingbat
# forms (➀-➄, ❶-❺, ⓵-⓹) depending on the embedded font.
CIRCLED_MAP = {}
for _i, _ch in enumerate("①②③④⑤", 1):
    CIRCLED_MAP[_ch] = _i
for _i, _ch in enumerate("➀➁➂➃➄", 1):
    CIRCLED_MAP[_ch] = _i
for _i, _ch in enumerate("❶❷❸❹❺", 1):
    CIRCLED_MAP[_ch] = _i
for _i, _ch in enumerate("⓵⓶⓷⓸⓹", 1):
    CIRCLED_MAP[_ch] = _i
CIRCLED_CLASS = "[" + "".join(CIRCLED_MAP) + "]"

def extract_answer_key(ans_text):
    """Return dict 1..45 -> circled int, or None if nothing found.

    Two passes. Pass 1 accepts lines carrying at least 3 number/circled
    pairs, in either space form ("1 ③ 2 ① ...", pdftotext) or markdown
    table form ("| 1 | ③ | 2 | ① | ...", kordoc/OCR). Pass 2 fills
    remaining gaps from bare "NN ③" lines (OCR fragments of the key
    grid); it can only add missing numbers, never override pass 1.
    Circled digits are accepted in every Unicode block the parsers
    produce (see CIRCLED_MAP).
    """
    key = {}
    for line in ans_text.splitlines():
        pairs = re.findall(r"(\d{1,2})\s+(" + CIRCLED_CLASS + r")", line)
        pairs += re.findall(r"(\d{1,2})\s*\|\s*(" + CIRCLED_CLASS + r")", line)
        # only accept lines that start (after spaces) with a number and contain >=2 pairs, and are near 정답?
        # To avoid picking up question options, require line to have at least 3 pairs and no Korean explanation before first pair
        if len(pairs) >= 3:
            # check the line does not look like a question option line (options are single)
            for num, circ in pairs:
                n = int(num)
                if 1 <= n <= 45 and n not in key:
                    key[n] = CIRCLED_MAP[circ]
    if len(key) < 45:
        for line in ans_text.splitlines():
            m = re.fullmatch(r"\s*[-–]?\s*(\d{1,2})\s+(" + CIRCLED_CLASS + r")\s*", line)
            if m:
                n = int(m.group(1))
                if 1 <= n <= 45 and n not in key:
                    key[n] = CIRCLED_MAP[m.group(2)]
    return key if key else None

def extract_arrow_corrections(ans_text):
    pairs = []
    for m in re.finditer(r"([A-Za-z][A-Za-z\-']+(?: [A-Za-z\-']+)?)\s*→\s*([A-Za-z]+(?: [A-Za-z]+){0,2})", ans_text):
        w, c = m.group(1).strip(), m.group(2).strip()
        # trim trailing common words that are not part of phrase? keep as is, but strip if c ends with '으로' etc - not in English pattern
        if w.lower() != c.lower():
            pairs.append((w, c))
    return pairs

def extract_appropriate_words(ans_text):
    """English words the explanation calls appropriate ("X이/가 적절하다").

    Used only as a strictly gated fallback for grammar/vocab items with
    no arrow pair: the caller applies the word only when exactly one
    such word exists in the whole answer text.
    """
    return re.findall(r"([A-Za-z][A-Za-z\-']+)(?:이|가) 적절하다", ans_text)

# Korean prompt words that column line breaks can split ("고르 시오").
# Joining them before the prompt/passage split keeps the split regex
# working; the same list is applied to the final prompt in build_exam.
PROMPT_KNOWN_WORDS = ["고르시오","적절한","가장","일치하지","일치하는","내용","제목","주제","요지","목적","심경","변화","주장","드러난","의미하는","관계","없는","문장","순서","배열","빈칸","들어갈","요약","하고자","다음","글의","중에서","밑줄","부분","어법상","틀린","문맥상","낱말","쓰임","적절하지","않은","것은","것을","가리키는","대상","나머지","다른","주어진","이어질","맞게","배열한","것으로","관한","안내문의","일치하는","상황","설명","듣고","대화를","여자가","남자가","할","일로","이유를","직원에게"]

def join_prompt_words(text):
    """Rejoin prompt words split by a column line break.

    The split fragment must stand alone: without word-boundary guards
    the fragment of one known word can span two unrelated words
    ("다 음" from "다음" glues "북보다 음악을" into "북보다음악을";
    "문 장" from "문장" glues "방문 장소" into "방문장소"). The
    fragments are therefore joined only when neither side touches a
    Korean syllable or a Latin letter.
    """
    for _w in PROMPT_KNOWN_WORDS:
        for _i in range(1, len(_w)):
            _frag = _w[:_i] + " " + _w[_i:]
            text = re.sub(
                r"(?<![가-힣A-Za-z])" + re.escape(_frag) + r"(?![가-힣A-Za-z])",
                _w, text)
    return text

def strip_space_before_punct(text):
    """Drop column-join spaces before punctuation.

    "!" and "?" keep their space when directly followed by a letter:
    "!Kung" is a name, and the space before it is real ("individual
    !Kung", not "individual!Kung"). Closing quotes attach to the left.
    """
    text = re.sub(r"\s+([,.;:”’])", r"\1", text)
    text = re.sub(r"\s+([!?])(?=\s|$|[”’\"'!?])", r"\1", text)
    return text

def is_korean_only_answer(*texts):
    """True when the answer material is a Korean-subject key, not English.

    The 2026_M9 repo answer PDFs contain only Korean-subject content;
    their key tables must never drive English restoration. Bundled
    PDFs (English first, Korean later) contain both markers and are
    not affected.
    """
    combined = "\n".join(texts)
    has_kor = ("국어 영역" in combined) or ("국어영역" in combined)
    has_eng = ("영어 영역" in combined) or ("영어영역" in combined)
    return has_kor and not has_eng

def join_chunks(block_lines):
    """Join wrapped lines into paragraphs: blank line = paragraph break."""
    paras = []
    cur = []
    for s in block_lines:
        if not s.strip():
            if cur:
                paras.append(" ".join(cur).strip())
                cur = []
        else:
            cur.append(s.strip())
    if cur:
        paras.append(" ".join(cur).strip())
    out = []
    for p in paras:
        # long space runs in column text are blank markers (e.g. "wear.                helps")
        p = re.sub(r" {10,}", " ____ ", p)
        p = re.sub(r"\s+", " ", p)
        p = fix_hyphens(p)
        out.append(p)
    return out

def _phrase_pattern(w):
    """Regex for a correction phrase, tolerating a circled marker
    between its words ("to ③ count", "have ③ taught")."""
    return r"\s*(?:[①②③④⑤]\s*)?".join(re.escape(p) for p in w.split())

def _replace_at_marked(text, w, c):
    """Replace the old word with the correction at its marked span.

    A blind first-occurrence replace hits unmarked earlier occurrences
    ("knows that the hard part" instead of the marked ⑤ "that"). When
    exactly one marked span holds the old word, splice there; for a
    multi-word phrase, take the match overlapping a marked span; else
    fall back to the first occurrence.
    """
    spans = [(m.start(1), m.end(1)) for m in re.finditer(r"[①②③④⑤]\s*([A-Za-z\-']+)", text)]
    if not spans:
        spans = [(m.start(1), m.end(1)) for m in re.finditer(r"\([a-e]\)\s*([A-Za-z\-']+)", text)]
    if len(w.split()) == 1:
        cands = [(s, e) for (s, e) in spans if text[s:e] == w]
        if len(cands) == 1:
            s, e = cands[0]
            return text[:s] + c + text[e:]
        return re.sub(rf"\b{re.escape(w)}\b", c, text, count=1)
    pat = _phrase_pattern(w)
    ms = list(re.finditer(rf"\b{pat}\b", text))
    over = [m for m in ms if any(not (m.end() <= s or m.start() >= e) for (s, e) in spans)]
    if len(over) == 1:
        m = over[0]
        return text[:m.start()] + c + text[m.end():]
    if len(ms) == 1:
        m = ms[0]
        return text[:m.start()] + c + text[m.end():]
    return re.sub(rf"\b{pat}\b", c, text, count=1)

def strip_markers(text):
    """Remove circled numbers and parenthesis segment labels from passage text."""
    # circled numbers as standalone tokens
    text = re.sub(r"\s*[①②③④⑤]\s*", " ", text)
    # segment labels (A) (B) (C) (D) at paragraph starts or standalone, and (a)-(e)
    text = re.sub(r"^\s*\([A-D]\)\s*", "", text, flags=re.M)
    text = re.sub(r"\s*\([A-D]\)\s*", " ", text)
    text = re.sub(r"\s*\([a-e]\)\s*", " ", text)
    # insertion markers ( ① ) etc already handled by circled removal, also "( ① )"
    text = re.sub(r"\(\s*\)", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()

def drop_summary_sentence(passage):
    """Remove the Q40 summary sentence (the one carrying the (A) blank).

    The restored format keeps the passage only; the summary sentence
    follows the passage and always contains the (A) blank. Cut at the
    last sentence boundary before the first "(A)" so both the
    separate-paragraph and merged cases are handled. Returns the
    passage unchanged when no "(A)" is present.
    """
    idx = passage.find("(A)")
    if idx < 0:
        return passage
    head = passage[:idx]
    bounds = [m.end() for m in re.finditer(r"[.!?][”\"]?\s+|\n\n+", head)]
    if bounds:
        return passage[:bounds[-1]].rstrip()
    return head.rstrip()

def split_speaker_options(paras):
    """Split dialogue speaker labels from glued option lists (1-17).

    A paragraph like "Man: ① ... ② ... ③ ..." is a speaker label plus
    the option list. Returns (passage_paras, option_paras). Only splits
    when the text before the first circled marker is a short label and
    at least 3 circled markers follow.
    """
    out_paras = []
    extra_opts = []
    for p in paras:
        m = re.search(r"[①②③④⑤]", p)
        if m and m.start() > 0:
            head, tail = p[:m.start()].strip(), p[m.start():]
            n_mark = sum(tail.count(c) for c in "①②③④⑤")
            if n_mark >= 3 and len(head) <= 40 and (
                head.endswith(":") or re.fullmatch(r"[A-Za-z][A-Za-z .'-]*", head)
            ):
                if head:
                    out_paras.append(head)
                extra_opts.append(tail)
                continue
        out_paras.append(p)
    return out_paras, extra_opts

def _table_header(lines, table_start):
    """Nearest preceding plain item header within 30 lines.

    Returns (qnum, header_idx) or (None, None). kordoc sometimes
    prefixes the real header with a spurious list number
    ("1. 19\\. ..."); the second number is the item number.
    """
    for k in range(table_start - 1, max(-1, table_start - 31), -1):
        hm = re.match(r"\s*(\d{1,2})\.\s+(.*)$", lines[k])
        if hm:
            am = re.match(r"(\d{1,2})\\?\.\s+", hm.group(2))
            return (int(am.group(1)) if am else int(hm.group(1))), k
    return None, None

def _table_title(lines, header_idx, table_start):
    """Title from the header line remainder plus lines up to the table."""
    title_parts = []
    hline = re.sub(r"<[^>]+>", "", lines[header_idx])
    hm2 = re.match(r"\s*\d{1,2}\.\s+(.*)$", hline)
    rest = hm2.group(1).strip() if hm2 else ""
    # The title (if any) follows the prompt on the header line.
    pm = re.search(r"(고르시오\.|것은\?|것인가\?|고르시오\?)", rest)
    if pm:
        tail = rest[pm.end():].strip()
        if tail and re.search(r"[A-Za-z0-9가-힣]", tail):
            title_parts.append(tail)
    for ln in lines[header_idx + 1:table_start]:
        clean = re.sub(r"<[^>]+>", "", ln).strip()
        if clean and re.search(r"[A-Za-z0-9가-힣]", clean):
            title_parts.append(clean)
    return " ".join(title_parts).strip()

def _clean_cell(text):
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\\$", "$").replace("＄", "$")
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return text.strip()

def _merge_cells(cells):
    """Join colspan-split cells: digit+unit and currency+number join
    without a space ("150"+"g" -> "150g", "$"+"10" -> "$10"); other
    splits join with a space."""
    out = cells[0]
    for nxt in cells[1:]:
        if (out and out[-1].isdigit() and nxt and nxt[0].isalpha()) or (
            out in ("$", "＄") and nxt and nxt[0].isdigit()
        ):
            out += nxt
        else:
            out = (out + " " + nxt).strip()
    return out

def _parse_html_table(region):
    """Parse one <table>...</table> region into (caption, markdown text).

    Handles kordoc's HTML table forms: colspan-split cells
    ("150"+"g", "$"+"10"), and circled markers in lone rows that
    pair with data rows in order. Returns None when the table is not
    an option table (>=3 data rows with markers ①-⑤ in order).
    Leading caption rows (a single spanning cell) are dropped from
    the table and returned as the caption for title fallback.
    """
    rows = []
    for rm in re.finditer(r"<tr[^>]*>(.*?)</tr>", region, flags=re.S):
        cells = []
        for cm in re.finditer(r"<t[hd]([^>]*)>(.*?)</t[hd]>", rm.group(1), flags=re.S):
            span_m = re.search(r'colspan\s*=\s*"(\d+)"', cm.group(1))
            cells.append((_clean_cell(cm.group(2)), int(span_m.group(1)) if span_m else 1))
        if cells:
            rows.append(cells)
    # Drop leading caption rows: a single non-empty cell spanning
    # multiple columns (e.g. "<th colspan=5>White Noise Machine</th>"
    # above the real header). Without this the caption is mistaken for
    # the header and the column counts never line up.
    captions = []
    while len(rows) > 1:
        non_empty = [(c, s) for c, s in rows[0] if c]
        if len(non_empty) == 1 and non_empty[0][1] > 1:
            captions.append(non_empty[0][0])
            rows = rows[1:]
        else:
            break
    if len(rows) < 4:  # header + >=3 data rows (incl. lone-marker rows)
        return None
    header = rows[0]
    spans = [s for _, s in header]
    header_cells = [c for c, _ in header]
    body = rows[1:]
    # Separate lone-marker rows from content rows.
    markers = []
    content = []
    for cells in body:
        texts = [c for c, _ in cells]
        if len(texts) == 1 and texts[0] in "①②③④⑤":
            markers.append(texts[0])
        else:
            content.append(cells)
            if texts and texts[0] in "①②③④⑤":
                markers.append(texts[0])
    if len(content) < 3 or len(markers) < 3:
        return None
    # Markers must pair with content rows in ①-⑤ order.
    want = "①②③④⑤"[:len(content)]
    if "".join(markers[:len(content)]) != want:
        return None
    # Group content cells per header colspans, then ensure the marker
    # is the first cell of each row.
    merged = []
    for idx, cells in enumerate(content):
        texts = [c for c, _ in cells]
        if sum(spans) == len(texts) and len(spans) < len(texts):
            grouped = []
            pos = 0
            for sp in spans:
                grouped.append(_merge_cells(texts[pos:pos + sp]))
                pos += sp
            texts = grouped
        if not texts or texts[0] != want[idx]:
            texts = [want[idx]] + texts
        merged.append(texts)
    ncols = len(merged[0])
    if any(len(r) != ncols for r in merged):
        return None
    if len(header_cells) == ncols - 1:
        header_cells = [""] + header_cells
    elif len(header_cells) != ncols:
        return None
    def fmt(cells):
        return "| " + " | ".join(cells) + " |"
    out = [fmt(header_cells), "|" + " --- |" * ncols]
    out += [fmt(r) for r in merged]
    return " ".join(captions), "\n".join(out)

def extract_cols_table(body_lines):
    """Parse a columnar text table from pdftotext -layout body lines.

    Fallback for items whose kordoc parse holds no table (garbled
    pages): the column text keeps the table as aligned rows, e.g.
    "     Model    Price     Operation" followed by " ①    A    $20
    Manual ...". Returns (title, markdown table text) or None.
    Gates: >=3 marker-led rows with equal field counts (>=2 data
    fields) and a header above with the matching field count. Header
    cells may wrap over several lines ("Tank" / "Volume" over two
    lines); wrapped headers are assembled by assigning each header
    word to the data column its centre falls in. Option lists never
    satisfy the field-count gates.
    """
    marker_re = re.compile(r"\s*([①②③④⑤])\s+")

    def _deblank(ln):
        # Centred title/header lines carry a "____ " prefix from the
        # indent rule in clean_lines; inside a table region they are
        # captions, not blanks.
        return re.sub(r"^____\s+", "", ln.strip())

    def _is_title(cand):
        # Single-spaced short English phrase. Multi-space gaps mean a
        # column-aligned header line, not a title.
        return (len(cand) <= 60
                and re.fullmatch(r"[A-Za-z0-9'’‘]+( [A-Za-z0-9'’‘&/\-.,()]+)*", cand)
                and not re.search(r"[가-힣]", cand))

    def _spans(ln):
        """[(start, text)] data fields of one marker-led row, absolute offsets."""
        m = marker_re.match(ln)
        if not m:
            return None
        out = []
        pos = m.end()
        for chunk in re.split(r"( {2,})", ln[m.end():]):
            if not chunk:
                continue
            if chunk.startswith("  "):
                pos += len(chunk)
            else:
                out.append((pos, chunk.strip()))
                pos += len(chunk)
        return out

    # Runs of marker-led rows, allowing blank lines between rows.
    row_idx = [i for i, ln in enumerate(body_lines) if marker_re.match(ln)]
    runs = []
    for i in row_idx:
        if runs and all(not body_lines[k].strip() for k in range(runs[-1][-1] + 1, i)):
            runs[-1].append(i)
        else:
            runs.append([i])

    for run in runs:
        if len(run) < 3:
            continue
        parsed = [_spans(body_lines[i]) for i in run]
        if any(p is None for p in parsed):
            continue
        nfields = len(parsed[0])
        if nfields < 2 or any(len(p) != nfields for p in parsed):
            continue
        markers = "".join(marker_re.match(body_lines[i]).group(1) for i in run)
        if markers != "①②③④⑤"[:len(run)]:
            continue
        # Candidate header lines: consecutive non-empty lines above
        # the run, stopping at a Korean line (prompt continuation).
        # Keep both the raw line (column positions for assembly) and
        # the deblanked text (title/simple-path checks).
        hdr_lines = []
        hi = run[0] - 1
        while hi >= 0 and len(hdr_lines) < 4:
            raw_ln = body_lines[hi]
            ln = _deblank(raw_ln)
            if not ln:
                hi -= 1
                continue
            if re.search(r"[가-힣]", ln):
                break
            hdr_lines.append((raw_ln, ln))
            hi -= 1
        hdr_lines.reverse()
        if not hdr_lines:
            continue
        # Title: the topmost candidate line, when it is a single-spaced
        # phrase and the lines below it still form a header.
        title = ""
        if len(hdr_lines) > 1 and _is_title(hdr_lines[0][1]):
            title = hdr_lines[0][1]
            hdr_lines = hdr_lines[1:]
        # Fast path: the nearest header line splits into the field count.
        header_cells = None
        simple = [p.strip() for p in re.split(r" {2,}", hdr_lines[-1][1]) if p.strip()]
        if len(simple) == nfields and len(hdr_lines) == 1:
            header_cells = simple
        else:
            # Assemble wrapped headers by word-to-column assignment
            # on the raw lines (leading spaces carry the positions;
            # "____ "-prefixed lines lost theirs and are skipped).
            starts = []
            for k in range(nfields):
                col = sorted(p[k][0] for p in parsed)
                starts.append(col[len(col) // 2])
            cells = [[] for _ in range(nfields)]
            for raw_ln, _ in hdr_lines:
                if raw_ln.strip().startswith("____ "):
                    continue
                for wm in re.finditer(r"\S+", raw_ln):
                    centre = wm.start() + len(wm.group()) / 2
                    col = 0
                    for k in range(nfields):
                        if centre >= starts[k]:
                            col = k
                    cells[col].append(wm.group())
            if any(not c for c in cells):
                continue
            header_cells = [" ".join(c) for c in cells]
        if len(header_cells) != nfields or any(not c for c in header_cells):
            continue

        def fmt(cells):
            return "| " + " | ".join(cells) + " |"
        out = [fmt([""] + header_cells), "|" + " --- |" * (nfields + 1)]
        out += [fmt([marker_re.match(body_lines[i]).group(1)]
                    + [t for _, t in _spans(body_lines[i])]) for i in run]
        return title, "\n".join(out)
    return None


def repair_table_cells(table_text, body_lines):
    """Fix kordoc cell artifacts using the same question's column text.

    - soft hyphens and hyphen variants (U+2010/U+2011) fold to "-"
      (same rule as passages; no restored table cell keeps them);
    - "$ 20" -> "$20", "built - in" -> "built-in", "&outdoor" ->
      "& outdoor" (spacing lost or added by the table parse);
    - hyphenation lost by kordoc ("Plugin") is restored from the
      hyphenated form in the column text ("Plug-in").
    """
    text = table_text.replace("\u00ad", "-").replace("\u2010", "-").replace("\u2011", "-")
    hyph_map = {}
    joined = "\n".join(body_lines)
    for w in re.findall(r"[A-Za-z]+(?:[-\u00ad\u2010\u2011][A-Za-z0-9]+)+", joined):
        folded = re.sub(r"[\u00ad\u2010\u2011]", "-", w)
        hyph_map.setdefault(re.sub(r"-", "", folded).lower(), folded)

    def _fix_row(line):
        if not line.lstrip().startswith("|"):
            return line
        line = re.sub(r"\$\s+(\d)", r"$\1", line)
        line = re.sub(r"([A-Za-z]) - ([A-Za-z])", r"\1-\2", line)
        line = re.sub(r"(?<=[\s|])&(?=[A-Za-z])", "& ", line)

        def _word(m):
            w = m.group(0)
            return hyph_map.get(w.lower(), w) if len(w) >= 3 else w
        return re.sub(r"[A-Za-z]+", _word, line)
    return "\n".join(_fix_row(ln) for ln in text.splitlines())


def extract_md_tables(problem_md_text):
    """Find option tables in a kordoc problem parse.

    Returns {qnum: (title, table_text)}. Tables appear as markdown
    pipe blocks or HTML <table> blocks. A table is assigned to the
    nearest preceding plain item header within 30 lines, and only
    kept when it holds the item's option set (at least 3 circled
    markers, in the table or in a marker list right below it) and
    the item number is <= 17. Titles come from the header line
    remainder plus lines between the header and the table.
    """
    lines = problem_md_text.splitlines()
    tables = {}

    def _clean_pipe_line(ln):
        return re.sub(r"<[^>]+>", "", ln).replace("\\$", "$").rstrip()

    i = 0
    while i < len(lines):
        stripped = lines[i].lstrip()
        if stripped.startswith("|"):
            j = i
            block = []
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                block.append(lines[j].rstrip())
                j += 1
            sep_idx = next(
                (k for k, ln in enumerate(block) if re.match(r"\|\s*[-:]+", ln.lstrip())),
                None,
            )
            if sep_idx is not None and len(block) - sep_idx - 1 >= 3:
                joined = "\n".join(block)
                n_circ = sum(joined.count(c) for c in "①②③④⑤")
                use_block = None
                if n_circ >= 3:
                    use_block = [_clean_pipe_line(ln) for ln in block]
                else:
                    # Markers listed below the table ("- ①", ...):
                    # merge them as the first column when the counts
                    # match the data rows.
                    data_n = len(block) - sep_idx - 1
                    markers = []
                    for ln in lines[j:j + 12]:
                        mm = re.match(r"\s*[-•]?\s*([①②③④⑤])\s*$", ln)
                        if mm:
                            markers.append(mm.group(1))
                    if len(markers) == data_n and "".join(markers) == "①②③④⑤"[:data_n]:
                        rebuilt = []
                        for k, ln in enumerate(block):
                            clean = _clean_pipe_line(ln)
                            prefix, _, rest = clean.partition("|")
                            if k < sep_idx:
                                cell = "  "
                            elif k == sep_idx:
                                cell = " --- "
                            else:
                                cell = f" {markers[k - sep_idx - 1]} "
                            rebuilt.append(prefix + "|" + cell + "|" + rest)
                        use_block = rebuilt
                if use_block is not None:
                    qnum, header_idx = _table_header(lines, i)
                    if qnum is not None and 1 <= qnum <= 17 and qnum not in tables:
                        tables[qnum] = (_table_title(lines, header_idx, i), "\n".join(use_block))
            i = j
        elif "<table" in stripped:
            j = i
            region_lines = []
            while j < len(lines) and "</table>" not in lines[j]:
                region_lines.append(lines[j])
                j += 1
            if j < len(lines):
                region_lines.append(lines[j])
                j += 1
            table_parsed = _parse_html_table("\n".join(region_lines))
            if table_parsed is not None:
                caption, table_text = table_parsed
                qnum, header_idx = _table_header(lines, i)
                if qnum is not None and 1 <= qnum <= 17 and qnum not in tables:
                    title = _table_title(lines, header_idx, i) or caption
                    tables[qnum] = (title, table_text)
            i = j
        else:
            i += 1
    return tables

def normalize_passage_text(text):
    """Mechanical normalizations for assembled passage text.

    - Stray spaces before punctuation ("word ." -> "word."), an artifact
      of column line joins.
    - Poster bullet markers (⦁ ∙ ◈ ▪ ㆍ ⚫ ✓), dropped in the restored
      format. ※ and • are kept: the restored format retains them as
      note markers and separators in notice items.
    - Doubled prepositions before "which" ("into into which"), an
      artifact of earlier restoration; never grammatical when doubled.
    - Runs of spaces on non-table lines (column-join artifacts).
      Table rows keep their spacing: empty cells are two spaces.
    """
    collapsed = []
    for ln in text.split("\n"):
        if ln.lstrip().startswith("|"):
            collapsed.append(ln)
        else:
            collapsed.append(re.sub(r" {2,}", " ", ln))
    text = "\n".join(collapsed)
    text = strip_space_before_punct(text)
    # List-bullet markers the restored format drops (▪ ㆍ ⚫ ⦁ ∙ ◈ ✓).
    # ※ and • are kept: the restored format retains them as note
    # markers and separators in notice items.
    text = re.sub(r"[⦁∙◈▪ㆍ⚫✓✔]\s*", "", text)
    text = re.sub(
        r"\b(into|through|in|on|of|to|for|with|from|by|at|over|under)"
        r"\s+\1(\s+which\b)",
        r"\1\2",
        text,
    )
    return text

def order_segments(text, letters, join_sep="\n\n"):
    """Reorder (A)-(D) segments per the answer letters.

    Segments not mentioned in the letters (e.g. the given (A) lead in
    Q43, whose options order only (B)/(C)/(D)) stay first in their
    original order; dropping them was the Q43-45 lead-loss defect.
    Returns (ordered_text, segment_labels), or (None, labels) when the
    labels do not cover the letters.
    """
    parts = re.split(r"\(([A-D])\)", text)
    seg = {}
    intro = parts[0].strip() if parts else ""
    for idx in range(1, len(parts) - 1, 2):
        seg[parts[idx]] = parts[idx + 1].strip()
    letter_set = set(letters)
    if not letter_set <= set(seg.keys()):
        return None, list(seg.keys())
    ordered = []
    if intro:
        ordered.append(intro)
    for kl in seg.keys():
        if kl not in letter_set:
            ordered.append(seg[kl])
    for l in letters:
        if l in seg:
            ordered.append(seg[l])
    return join_sep.join(ordered), list(seg.keys())

def split_questions(lines, log=None):
    """
    Return ordered dict: qnum -> {'prompt': str, 'body_lines': [], 'group_passage': str or None, 'group_range': tuple or None}
    and group_passages: {(start,end): passage_text_raw}
    Strategy: walk lines, when group heading found, collect following lines as group passage
    until first question number in range. Then questions in range share that passage.
    For non-group questions, body is lines until next question/group heading.
    Header numbers are repaired from sequence context: a leading digit
    lost at the column boundary reads "0."/"1."/"8." for 10/11/18 etc.
    A header whose number plus a multiple of 10 equals the next expected
    number is repaired to it; anything else keeps the old behaviour.
    """
    questions = {}
    groups = {}  # (start,end) -> raw lines
    groups_prompt = {}
    cur_group = None
    cur_q = None
    expected = 1  # next question number in document order
    # first pass: collect raw blocks
    blocks = {}  # qnum -> list of lines (prompt line + body)
    order = []
    pending_group_lines = []
    i = 0
    while i < len(lines):
        line = lines[i]
        g = is_group_heading(line)
        if g:
            start, end = int(g.group(1)), int(g.group(2))
            cur_group = (start, end)
            # group prompt text after ] is stored as group instruction, not a question
            # collect passage lines until first q in range
            # group prompt: remainder of heading line + following wrapped lines
            # (indented continuation lines before a blank line / question header)
            gp = [g.group(3).strip()] if g.group(3).strip() else []
            passage_lines = []
            i += 1
            # continuation of group prompt: only if heading is incomplete (does not end with 시오./?/것은?) and next line is short Korean
            _heading = " ".join(gp)
            _complete = _heading.endswith("시오.") or _heading.endswith("?") or _heading.endswith("것은?")
            if not _complete:
                while i < len(lines) and lines[i].strip() and not q_header(lines[i]) and not is_group_heading(lines[i]):
                    _ln = lines[i].strip()
                    # stop if line looks like English passage start (capital letter, long)
                    if len(_ln) > 100 or (_ln and _ln[0].isupper() and any(c.isalpha() for c in _ln)):
                        # Korean continuation lines are Korean, English passage lines start with capital and are long English
                        # Check if line contains Korean: if not, it's passage, stop
                        import re as _re
                        if not _re.search(r"[가-힣]", _ln):
                            break
                    gp.append(_ln)
                    i += 1
                    # after one continuation line, if gp now ends with complete ending, stop
                    if " ".join(gp).endswith("시오.") or " ".join(gp).endswith("?"):
                        break
            # skip blank lines, then collect passage until ANY question header or next group
            while i < len(lines):
                lh = q_header(lines[i])
                if lh:
                    break
                if is_group_heading(lines[i]):
                    break
                passage_lines.append(lines[i])
                i += 1
            groups[cur_group] = passage_lines
            groups_prompt[cur_group] = " ".join(gp).strip()
            continue
        h = q_header(line)
        # Reject headers that are mid-sentence continuations, e.g. "April\n20. The rental fee..." where 20 is a date, not a question number
        if h and i > 0:
            _prev = lines[i-1].strip() if i-1 < len(lines) else ""
            _is_footer = (any(w in _prev for w in ("문제지", "학력평가", "영역", "영어", "고1", "고2", "고3")) or _prev.isdigit())
            if _prev and not _is_footer and not _prev.endswith((".", "?", "!", "”", "’", "…")) and not re.match(r"^[①②③④⑤]", _prev) and not is_group_heading(lines[i-1]):
                # Previous line is a mid-sentence passage line, not a blank/option/footer -> not a header
                h = None
        if h:
            n, prompt = h
            # Sequence repair for clipped leading digits: "0." for 10/20/
            # 30/40, "1." for 11/21/31/41, etc. Only fires when the number
            # plus a multiple of 10 is exactly the next expected number,
            # so genuine duplicates and out-of-order headers are untouched.
            if n < expected and (expected - n) % 10 == 0:
                if log is not None:
                    log.append(f"header repaired: {n}. -> {expected}. "
                               f"({prompt[:50]})")
                n = expected
            if n == 0:
                # A "0." line that is not a clipped 10/20/30/40 header is
                # stray text, not a question.
                i += 1
                continue
            # avoid duplicate headers from footers / answer table etc: if already seen and this is a later duplicate with same number, keep first
            if n in blocks:
                # Numbered-list item inside a notice (e.g. "2. Check out
                # ..." in a "1./2./3./4." how-to list): the number
                # duplicates an earlier question header, but the list
                # context proves it is body text. Only the nearest
                # numbered line counts, and real 1-9 headers never occur
                # once cur_q has passed 10.
                if cur_q is not None and cur_q > 10:
                    _list_ctx = False
                    _back = 0
                    _j = i - 1
                    while _j >= 0 and _back < 3:
                        _pl = lines[_j].strip()
                        if _pl:
                            _back += 1
                            _lm = re.match(r"(\d{1,2})\.\s", _pl)
                            if _lm:
                                _m = int(_lm.group(1))
                                if _m < 10 and 1 <= n - _m <= 3:
                                    _list_ctx = True
                                break
                        _j -= 1
                    if _list_ctx:
                        blocks[cur_q].append(line)
                # duplicate: append as body to current? skip header line
                i += 1
                continue
            blocks[n] = [prompt]
            order.append(n)
            cur_q = n
            expected = n + 1
            i += 1
            continue
        # regular line
        if cur_q is not None:
            blocks[cur_q].append(line)
        elif cur_group is not None:
            # lines before first q in group but after group heading already handled; ignore
            pass
        i += 1
    # Build questions
    for n in order:
        raw_lines = blocks[n]
        prompt = raw_lines[0] if raw_lines else ""
        body = raw_lines[1:]
        # find group for this q
        grp = None
        for (s, e), plines in groups.items():
            if s <= n <= e:
                grp = (s, e)
                break
        questions[n] = {"prompt": prompt, "body_lines": body, "group": grp}
    return questions, groups, groups_prompt

def extract_options(body_lines):
    """Return (options_list, remaining_lines). Options are lines starting with circled number, or table rows."""
    opts = []
    rest = []
    for s in body_lines:
        st = s.strip()
        if re.match(r"^[①②③④⑤]\s", st) or re.match(r"^[①②③④⑤]$", st):
            opts.append(st)
        elif re.match(r"^\|\s*[①②③④⑤]", st):
            opts.append(st)
        else:
            # option lines that contain multiple circled numbers on one line, e.g. "① ... ② ..." for 19,40 etc - treat as options if line starts with circled and contains at least 2
            if re.match(r"^[①②③④⑤]", st) and st.count("①")+st.count("②")+st.count("③")+st.count("④")+st.count("⑤") >= 2:
                opts.append(st)
            else:
                rest.append(s)
    return opts, rest

def parse_options_text(opts):
    """Parse option lines into dict 1..5 -> text. Handles single-line and multi-line and table."""
    opt = {}
    # join all opts, then split by circled
    joined = " ".join(opts)
    # table case: | ① | A | $31 | ... -> keep as table, return special
    if "|" in joined:
        return None
    parts = re.split(r"([①②③④⑤])", joined)
    # parts: ['', '①', ' text ', '②', ' text ', ...]
    for idx in range(1, len(parts)-1, 2):
        circ = parts[idx]
        txt = parts[idx+1].strip() if idx+1 < len(parts) else ""
        # cut at next table marker etc
        opt[CIRC_MAP[circ]] = txt
    return opt

def is_question_like(text):
    if not text:
        return False
    if len(text) > 300:
        return False
    # Korean question prompts
    if re.search(r"고르시오|것은\?|무엇|어느 것|순서로|빈칸|들어가기에|요약하고자|제목으로|주제로|요지로|목적으로|심경|주장하는|일치하지|일치하는|관계 없는|틀린 것은|적절하지 않은|의미하는|드러난", text):
        return True
    return False

def split_para_options(paras, allow_tail=True):
    opts = []
    non = []
    for p in paras:
        ps = p.strip()
        if re.fullmatch(r"[①②③④⑤\s]+", ps):
            continue
        # option para: starts with circled, or starts with (A)/(B) header then circled, or contains >=3 circled and is not a long passage
        circ_count = sum(ps.count(c) for c in "①②③④⑤")
        if re.match(r"^[①②③④⑤]", ps):
            opts.append(ps)
        elif ps.startswith("|") and "①" in ps:
            opts.append(ps)
        elif re.match(r"^\([A-D]\)(\s*\([A-D]\))*\s*[①②③④⑤]", ps):
            # strip leading (A) (B) header, keep options
            m = re.search(r"[①②③④⑤]", ps)
            opts.append(ps[m.start():] if m else ps)
        elif circ_count >= 3 and len(ps) < 600 and not re.search(r"\b[A-Z][a-z]{2,}\b.*\.", ps):
            opts.append(ps)
        else:
            if not allow_tail:
                non.append(p)
                continue
            # tail split: passage para ending with an option list (bare circled, no parens)
            tail_idx = None
            for m in re.finditer(r"[①②③④⑤]", ps):
                # skip markers in parentheses (insertion) and marked words in grammar (handled by allow_tail=False for those types)
                if m.start() > 0 and ps[m.start()-1] == "(":
                    continue
                rem = ps[m.start():]
                rc = sum(rem.count(c) for c in "①②③④⑤")
                if rc >= 3:
                    tail_idx = m.start()
                    break
            if tail_idx is not None and tail_idx > 100:
                non.append(ps[:tail_idx].strip())
                opts.append(ps[tail_idx:].strip())
            else:
                non.append(p)
    return opts, non

def apply_restoration(qnum, q, groups, groups_prompt, answer_key, arrow_pairs, log, exam_id, group_info=None, md_tables=None, appropriate_words=None):
    """Return (prompt, passage_text, options_or_None_for_1_17)."""
    # Reconstruct first paragraph from header remainder + body lines until blank
    full_lines = []
    if q["prompt"]:
        full_lines.append(q["prompt"])
    full_lines.extend(q["body_lines"])
    first_lines = []
    rest_lines = []
    seen_blank = False
    for ln in full_lines:
        if not seen_blank:
            if not ln.strip():
                seen_blank = True
            else:
                first_lines.append(ln.strip())
        else:
            rest_lines.append(ln)
    first_para = fix_hyphens(re.sub(r"\s+", " ", re.sub(r" {10,}", " ____ ", " ".join(first_lines))).strip())
    first_para = first_para.replace("", "").strip()
    first_para = join_prompt_words(first_para)
    rest_paras = join_chunks(rest_lines)
    grp = q.get("group")
    group_prompt = groups_prompt.get(grp, "") if grp else ""
    group_prompt = fix_hyphens(re.sub(r"\s+", " ", group_prompt).strip())
    # Split first_para into Korean prompt + English passage (or options) when prompt and passage share a paragraph
    split_m = re.search(r"^(.*?[가-힣].*?(?:고르시오\.|것은\?|문장은\?|것인가\?))(\s+\[3점\])?\s+([^가-힣\s].*)", first_para, flags=re.S)
    if split_m and len(split_m.group(1)) < 300:
        prompt = (split_m.group(1) + (split_m.group(2) or "")).strip()
        passage_start = split_m.group(3).strip()
        passage_paras_body = ([passage_start] if passage_start else []) + rest_paras
    elif is_question_like(first_para):
        prompt = first_para
        passage_paras_body = rest_paras
    elif group_prompt and is_question_like(group_prompt):
        prompt = group_prompt
        passage_paras_body = ([first_para] if first_para else []) + rest_paras
    else:
        prompt = first_para
        passage_paras_body = rest_paras
    # [3점] handling: move from passage to prompt
    has_3 = False
    new_body = []
    for p in passage_paras_body:
        if "[3점]" in p:
            has_3 = True
            p = p.replace("[3점]", "").strip()
            p = re.sub(r"\s+", " ", p)
        if p:
            new_body.append(p)
    passage_paras_body = new_body
    if has_3 and "[3점]" not in prompt:
        prompt = (prompt + " [3점]").strip()
    prompt = fix_hyphens(prompt)
    # Type detection early for tail-split control
    _pl = prompt
    _is_grammar = ("어법상 틀린" in _pl or "문맥상 낱말" in _pl) and ("가리키는" not in _pl) and ("의미하는" not in _pl)
    _is_insert = "들어가기에 가장 적절한 곳" in _pl or "주어진 문장이 들어가" in _pl
    _is_order = ("순서" in _pl and ("배열" in _pl or "이어질" in _pl or "다음에 이어질" in _pl))
    _is_irrelevant = "관계 없는" in _pl
    # Options from body paras. The irrelevant-sentence type (Q35) keeps
    # its ①-⑤ sentences as passage: splitting them off as options drops
    # them from the 18-45 output entirely (the Q35 truncation defect).
    if _is_irrelevant:
        opts, non_opt_paras = [], list(passage_paras_body)
    else:
        opts, non_opt_paras = split_para_options(passage_paras_body, allow_tail=not (_is_grammar or _is_insert or _is_order))
    # Group passage
    group_passage_paras = []
    if grp is not None and grp in groups:
        gp_lines = groups[grp]
        gp_paras_raw = join_chunks(gp_lines)
        # group passage may contain [3점] at end of a para - already handled? group passage for 36 etc contains [3점] in last para; move to prompt for that specific q? 
        # Actually [3점] in group passage belongs to the first question of group? For 36 group, [3점] is in 36 passage, not group passage (group passage empty). So ignore here, but strip [3점] from group paras and if q is first in group, add to prompt
        gp_clean = []
        for p in gp_paras_raw:
            if "[3점]" in p:
                p = p.replace("[3점]", "").strip()
                if qnum == grp[0] and "[3점]" not in prompt:
                    prompt = (prompt + " [3점]").strip()
            if p:
                gp_clean.append(p)
        _, gp_non = split_para_options(gp_clean, allow_tail=False)
        group_passage_paras = gp_non
        # Group-level restoration: if another question in same group is order/grammar, apply to shared passage for this question too
        if group_info and grp in group_info:
            gi = group_info[grp]
            # order: reorder segments in group passage text
            if gi.get("order") and qnum != gi.get("order_q"):
                gp_text = "\n\n".join(group_passage_paras)
                letters = gi["order"]
                _sep = " " if qnum in (36, 37) else "\n\n"
                ordered_text, _labels = order_segments(gp_text, letters, _sep)
                if ordered_text is not None:
                    group_passage_paras = [ordered_text]
            # corrections: apply to non-grammar questions in group (grammar q handles itself)
            if not _is_grammar:
                for (w,c) in gi.get("corrections", []):
                    group_passage_paras = [_replace_at_marked(pp, w, c) for pp in group_passage_paras]
    body_paras = non_opt_paras
    # For 1-17: keep options, passage is non-option paras (usually empty, except table/image cases handled by caller)
    if qnum <= 17:
        # Table items (Q10/Q12): the kordoc parse holds the option table
        # in proper markdown form; the column text flattens it. Use the
        # table plus its title, with no separate option lines. When the
        # kordoc parse holds no table, fall back to the aligned text
        # table in the column text. Titles are kept only for H1: the
        # restored H2/H3 files drop the table caption.
        table_src = None
        # A kordoc table is assigned to the nearest preceding header,
        # which misfires when the parse interleaves columns (a Q10 table
        # landing on Q9). Table items always mention 표 in the prompt;
        # a non-표 item never takes an md table.
        if md_tables and qnum in md_tables and "표" in prompt:
            table_src = md_tables[qnum]
        elif qnum <= 17 and "표" in prompt:
            table_src = extract_cols_table(q["body_lines"])
        if table_src is not None:
            title, table_text = table_src
            if exam_id.rsplit("_", 1)[-1] != "H1":
                title = ""
            table_text = repair_table_cells(table_text, q["body_lines"])
            passage = (title + "\n\n" + table_text) if title else table_text
            # The column text may glue the flattened table onto the
            # prompt; cut the prompt at its terminal punctuation.
            pm = re.search(r"^(.*?(?:고르시오\.|것은\?|것인가\?|고르시오\?))", prompt, flags=re.DOTALL)
            if pm:
                rest = prompt[pm.end():]
                prompt = pm.group(1)
                if re.match(r"\s*\[3점\]", rest):
                    prompt += " [3점]"
            return prompt, passage, []
        # Dialogue items: split a speaker label glued to the option list
        # ("Man: ① ... ② ...") into label + options.
        body_paras, extra_opts = split_speaker_options(body_paras)
        opts = opts + extra_opts
        # opts may be multi-option paras: split into individual options.
        # A bare marker with no text (picture-item labels "① ② ③ ④ ⑤")
        # carries no content and is dropped: the restored format keeps
        # no option lines for picture items.
        clean_opts = []
        for o in opts:
            # split by circled
            parts = re.split(r"([①②③④⑤])", o)
            for idx in range(1, len(parts)-1, 2):
                txt = parts[idx+1].strip() if idx+1 < len(parts) else ""
                if txt:
                    clean_opts.append(f"{parts[idx]} {txt}")
        # table options: if opts contain |, keep as table lines
        passage = "\n\n".join(body_paras)
        if "도표" in prompt:
            # Graph items: body text is graph furniture (caption, axis
            # labels). The restored format keeps only the image, the
            # manual table (phase 3), and the options.
            passage = ""
        return prompt, passage, clean_opts
    # 18-45
    opt_dict = parse_options_text(opts) if opts else {}
    # Combine group + body
    all_paras = []
    if group_passage_paras:
        all_paras.extend(group_passage_paras)
    all_paras.extend(body_paras)
    passage = "\n\n".join(all_paras)
    prompt_lower = prompt
    # Graph items (Q25): body text is graph furniture (caption, axis
    # labels). The restored format keeps only the image and the manual
    # table (phase 3); the caller emits the image refs.
    if "도표" in prompt_lower:
        return prompt, "", None
    is_order = ("순서" in prompt_lower and ("배열" in prompt_lower or "이어질" in prompt_lower or "다음에 이어질" in prompt_lower))
    is_insertion = "들어가기에 가장 적절한 곳" in prompt_lower or "주어진 문장이 들어가" in prompt_lower
    is_fill = "빈칸에 들어갈 말" in prompt_lower or "빈칸" in prompt_lower
    is_grammar_vocab = ("어법상 틀린" in prompt_lower or "문맥상 낱말" in prompt_lower) and ("가리키는" not in prompt_lower) and ("의미하는" not in prompt_lower)
    # --- Order ---
    if is_order and answer_key and qnum in answer_key:
        ans = answer_key[qnum]
        if not (opt_dict and ans in opt_dict):
            # Options glued to the last segment (no blank line between
            # them): recover the option sequences from the passage text
            # and strip them before segment splitting.
            _opt_re = re.compile(r"([①②③④⑤])\s*(\([A-D]\)(?:\s*-\s*\([A-D]\))+)", )
            _found = {CIRC_MAP[m.group(1)]: m.group(2) for m in _opt_re.finditer(passage)}
            if _found:
                for _k, _v in _found.items():
                    opt_dict.setdefault(_k, _v)
                passage = _opt_re.sub(" ", passage)
        if opt_dict and ans in opt_dict:
            opt_text = opt_dict[ans]
            letters = [l.strip("()") for l in re.findall(r"\([A-D]\)", opt_text)]
            if letters and len(letters) >= 2:
                _sep = " " if qnum in (36, 37) else "\n\n"
                passage2, seg_labels = order_segments(passage, letters, _sep)
                if passage2 is not None:
                    passage2 = strip_markers(passage2)
                    return prompt, passage2, None
                else:
                    log.append(f"{exam_id} Q{qnum}: order segments not found, labels={seg_labels}, expected={letters}")
            else:
                log.append(f"{exam_id} Q{qnum}: order option text not parsed: {opt_text}")
        else:
            log.append(f"{exam_id} Q{qnum}: order answer/options missing")
    if is_order:
        # Order handling above did not return (missing key/options):
        # strip option sequences so they do not survive as dash runs.
        passage = re.sub(r"[①②③④⑤]\s*\([A-D]\)(?:\s*-\s*\([A-D]\))+", " ", passage)
    # --- Insertion ---
    if is_insertion and answer_key and qnum in answer_key:
        ans = answer_key[qnum]
        # body_paras: first para is given sentence, rest is main passage.
        # When the given sentence runs straight into the passage with no
        # blank line between them, the paras collapse to one: split the
        # given sentence off textually at the first sentence boundary
        # (the given sentence is a single sentence and holds no markers;
        # the main passage must hold at least three).
        _given = None
        _main = None
        if len(body_paras) >= 2:
            _given = body_paras[0]
            _main = "\n\n".join(body_paras[1:])
        elif len(body_paras) == 1:
            _combined = body_paras[0]
            _bm = re.search(r"(?<!\be\.g)(?<!\bi\.e)(?<!\betc)(?<!\bDr)(?<!\bMr)(?<!\bMrs)(?<!\bMs)(?<!\bvs)[.!?][”’\"')]*\s+(?=[A-Z“\"(])", _combined)
            if _bm:
                _given = _combined[:_bm.end()].strip()
                _main = _combined[_bm.end():].strip()
                if sum(_main.count(c) for c in "①②③④⑤") < 3:
                    _given = None
                    _main = None
        if _given is not None:
            given = _given
            main = re.sub(r"\(\s*([①②③④⑤])\s*\)", r"\1", _main)
            segs = re.split(r"[①②③④⑤]", main)
            if len(segs) >= ans+1 or len(segs) >= ans:
                # segs[0]=before ①, segs[1]=between ①-②, ..., segs[ans]=after marker ans (if ans markers exist)
                # Insert given before segs[ans] (i.e. at position ans in segs list, 0-based: segs[:ans] + [given] + segs[ans:])
                # Check: answer ③ means insert at position ③, which is after segs[2] and before segs[3]
                if len(segs) > ans:
                    new_segs = segs[:ans] + [given] + segs[ans:]
                else:
                    new_segs = segs + [given]
                passage2 = "\n\n".join([s.strip() for s in new_segs if s.strip()])
                passage2 = strip_markers(passage2)
                return prompt, passage2, None
            else:
                log.append(f"{exam_id} Q{qnum}: insertion segments {len(segs)} < answer {ans}")
        else:
            log.append(f"{exam_id} Q{qnum}: insertion structure not recognized, paras={len(body_paras)}")
    # --- Fill (single blank) ---
    if is_fill and not ("요약" in prompt_lower):
        if answer_key and qnum in answer_key and opt_dict:
            ans = answer_key[qnum]
            if ans in opt_dict:
                # The option text itself may carry a stray blank marker
                # ("category ____") from the option-line parse; the
                # option IS the filler, so strip markers and collapse.
                fill_text = re.sub(r"\s+", " ", opt_dict[ans].replace("____", " ")).strip()
                if "____" in passage:
                    # Pad the filler: a marker glued to its neighbours
                    # ("us ____”") would otherwise glue the filler to
                    # them ("usindependently”"). The final clean
                    # collapses the extra spaces and reattaches
                    # punctuation and closing quotes.
                    passage = passage.replace("____", " " + fill_text + " ", 1)
                elif re.search(r"\s+\.", passage):
                    # A blank at a sentence end renders as a gap before
                    # the period ("The answer is ."). The first such gap
                    # is the site: 55 fill items rely on this path with
                    # no observed misplacement (the four suspected
                    # misplacements in 2024_M3_H1/H2 proved to be
                    # baseline-dropped fills at genuine gap sites).
                    passage = re.sub(r"\s+\.", f" {fill_text}.", passage, count=1)
                else:
                    # Invisible blank: the gap fell on a column line break
                    # and left no marker, only a doubled function word
                    # ("ready to to achieve"). Insert at the seam when
                    # exactly one such doubled word exists in the passage.
                    _words = r"to|a|an|the|and|or|of|in|on|for|with|by|at|from|into|over|under|is|are|was|were|be|been|it|as|than"
                    _found = [m for m in re.finditer(rf"\b({_words})\s+({_words})\b", passage, flags=re.IGNORECASE) if m.group(1).lower() == m.group(2).lower()]
                    if len(_found) == 1:
                        _m = _found[0]
                        passage = passage[:_m.end(1)] + " " + fill_text + " " + passage[_m.start(2):]
                    else:
                        log.append(f"{exam_id} Q{qnum}: fill blank marker not found, fill='{fill_text}', passage_end='{passage[-80:]}'")
                passage = strip_markers(passage)
                return prompt, passage, None
            else:
                log.append(f"{exam_id} Q{qnum}: fill option missing for answer {ans}")
    # --- Summary (40-type): passage only, summary sentence dropped ---
    if "요약" in prompt_lower:
        if "(A)" not in passage:
            log.append(f"{exam_id} Q{qnum}: no summary sentence found, passage kept whole")
        passage2 = drop_summary_sentence(passage)
        passage2 = strip_markers(passage2)
        return prompt, passage2, None
    # --- Grammar / vocab correction (per-question arrow pairs if available, else global) ---
    if is_grammar_vocab:
        pairs_to_use = arrow_pairs
        # arrow_pairs may be a dict {qnum: [(w,c),...]} if caller passes per-question, else list
        if isinstance(arrow_pairs, dict):
            pairs_to_use = arrow_pairs.get(qnum, [])
        marked = re.findall(r"[①②③④⑤]\s*([A-Za-z\-']+)", passage) + re.findall(r"\([a-e]\)\s*([A-Za-z\-']+)", passage)
        if marked:
            # A multi-word old side never equals a single marked word:
            # match it when the phrase occurs in the passage and any of
            # its words is marked (the marker can precede the first word
            # as in "② to count", or sit inside as in "have ③ taught").
            matches = [(w, c) for w, c in pairs_to_use
                       if w in marked or (len(w.split()) > 1
                                          and any(p in marked for p in w.split())
                                          and re.search(rf"\b{_phrase_pattern(w)}\b", passage))]
        else:
            matches = [(w, c) for w, c in pairs_to_use if re.search(rf"\b{_phrase_pattern(w)}\b", passage)]
        if len(matches) == 1:
            w,c = matches[0]
            # Prefer the marked word at the answer position; the helper
            # falls back to the unique marked span, then first occurrence.
            _done = False
            if len(w.split()) == 1 and answer_key and qnum in answer_key:
                _spans = [(m.start(1), m.end(1)) for m in re.finditer(r"[①②③④⑤]\s*([A-Za-z\-']+)", passage)]
                if not _spans:
                    _spans = [(m.start(1), m.end(1)) for m in re.finditer(r"\([a-e]\)\s*([A-Za-z\-']+)", passage)]
                _ans = answer_key[qnum]
                if 1 <= _ans <= len(_spans) and passage[_spans[_ans-1][0]:_spans[_ans-1][1]] == w:
                    _s, _e = _spans[_ans-1]
                    passage = passage[:_s] + c + passage[_e:]
                    _done = True
            if not _done:
                passage = _replace_at_marked(passage, w, c)
            passage = strip_markers(passage)
            return prompt, passage, None
        elif len(matches) > 1:
            log.append(f"{exam_id} Q{qnum}: grammar/vocab ambiguous matches {matches}, not applied")
        else:
            # Appropriate-word fallback: the explanation names the right
            # word ("X이/가 적절하다") without an arrow pair. Strict gates:
            # exactly one such word in the whole answer text, the answer
            # key covers this question, and the marked word at the answer
            # position exists. The replacement is spliced at the marked
            # word's exact span, never by blind first occurrence.
            # Two further gates stop cross-item splices: the word is a
            # word-FORM correction, so it applies only to grammar items
            # (어법상), never to vocabulary-choice items (문맥상 낱말,
            # whose corrections belong to the phase-3 dictionary).
            # (A stem-similarity gate was tried and removed: it blocked
            # the legitimate E correction what->that in 2024_M10_H1 Q29,
            # while the grammar-only gate alone already blocks every
            # observed sibling misfire on this corpus.)
            applied = False
            _is_grammar_only = ("어법상" in prompt_lower) and ("문맥상" not in prompt_lower)
            if (appropriate_words is not None and len(appropriate_words) == 1
                    and _is_grammar_only
                    and answer_key and qnum in answer_key):
                spans = [(m.start(1), m.end(1)) for m in re.finditer(r"[①②③④⑤]\s*([A-Za-z\-']+)", passage)]
                if not spans:
                    spans = [(m.start(1), m.end(1)) for m in re.finditer(r"\([a-e]\)\s*([A-Za-z\-']+)", passage)]
                ans = answer_key[qnum]
                if 1 <= ans <= len(spans):
                    s, e = spans[ans - 1]
                    old_word = passage[s:e]
                    new_word = appropriate_words[0]
                    if old_word.lower() != new_word.lower():
                        passage = passage[:s] + new_word + passage[e:]
                        log.append(f"{exam_id} Q{qnum}: appropriate-word applied: {old_word} -> {new_word} (answer {ans})")
                        applied = True
            if applied:
                passage = strip_markers(passage)
                return prompt, passage, None
            log.append(f"{exam_id} Q{qnum}: grammar/vocab no arrow match found, left original")
    passage = strip_markers(passage)
    return prompt, passage, None


def build_exam(stem, log, problem_pdf, work_dir):
    """stem like 2026_03_H3. problem_pdf is the question PDF path (used for
    pdfimages page mapping). Returns (output_text, images_info, exam_log)."""
    year_s, month_s, grade_s = stem.split("_")
    year = int(year_s); month = int(month_s); grade = grade_s  # H1 etc
    out_name = f"{year}_M{month}_{grade}_restored.md"
    workdir = work_dir / stem
    prob_cols_path = workdir / "problem_cols.txt"
    ans_cols_path = workdir / "answer_cols.txt"
    if not prob_cols_path.exists():
        log.append(f"{stem}: problem_cols.txt missing")
        return None, [], log
    prob_text = prob_cols_path.read_text(encoding="utf-8", errors="ignore")
    lines = clean_lines(prob_text)
    questions, groups, groups_prompt = split_questions(lines, log=log)
    # answer key: merge sources, preferring the column-split text
    # (first-wins per question). The kordoc/OCR parse fills gaps for
    # image-only answer PDFs; the full-width text is a last resort.
    answer_key = None
    arrow_pairs = []
    appropriate_words = []
    answer_type = "missing"
    ans_text = ""
    ans_md_text = ""
    ans_full_text = ""
    if ans_cols_path.exists():
        ans_text = ans_cols_path.read_text(encoding="utf-8", errors="ignore")
    ans_md_path = workdir / "answer.md"
    if ans_md_path.exists():
        ans_md_text = ans_md_path.read_text(encoding="utf-8", errors="ignore")
    ans_full_path = workdir / "answer_full.txt"
    if ans_full_path.exists():
        ans_full_text = ans_full_path.read_text(encoding="utf-8", errors="ignore")
    if ans_text or ans_md_text or ans_full_text:
        if is_korean_only_answer(ans_text, ans_md_text, ans_full_text):
            log.append(f"{stem}: answer PDF is Korean-subject only, key not usable")
            answer_type = "korean_only"
        else:
            merged = {}
            for src in (ans_text, ans_md_text, ans_full_text):
                if not src:
                    continue
                part = extract_answer_key(src)
                if part:
                    for n, v in part.items():
                        merged.setdefault(n, v)
                if len(merged) >= 45:
                    break
            answer_key = merged if merged else None
            # arrows: prefer kordoc answer.md, else full-width answer_full.txt (both avoid column truncation)
            arrow_src = ans_md_text or ans_full_text or ans_text
            arrow_pairs = extract_arrow_corrections(arrow_src)
            appropriate_words = extract_appropriate_words(arrow_src)
            if answer_key and len(answer_key) == 45:
                answer_type = "full_table"
            elif answer_key:
                answer_type = f"partial_table_{len(answer_key)}"
                log.append(f"{stem}: answer key partial {len(answer_key)}/45")
            else:
                answer_type = "no_table"
                log.append(f"{stem}: answer key not found in answer PDF")
    else:
        log.append(f"{stem}: answer PDF not a PDF (HTML) or missing, no answer key")
    # Check question numbers
    missing_q = [n for n in range(1,46) if n not in questions]
    if missing_q:
        log.append(f"{stem}: missing questions {missing_q}")
    # Images: kordoc images in workdir/images/problem/
    img_dir = workdir / "images" / "problem"
    img_files = []
    if img_dir.exists():
        img_files = sorted([p for p in img_dir.iterdir() if p.suffix.lower() in (".png",".jpg",".jpeg")])
    # Identify picture/graph questions from prompts (before restoration)
    # Use full prompt: first para (header + continuation) - reconstruct from raw block
    def full_prompt(q):
        lines = ([q["prompt"]] if q["prompt"] else []) + q["body_lines"]
        first = []
        for ln in lines:
            if not ln.strip():
                break
            first.append(ln.strip())
        return " ".join(first)
    pic_qs = []
    graph_qs = []
    for n in sorted(questions.keys()):
        # Use header prompt only, NOT full first paragraph: options may contain 그림/그래프 words (e.g. "그림과 글이 함께 포함된 플래시 카드", "그래프 제작하기") and would misclassify the question
        pr = questions[n]["prompt"] or ""
        if "그림" in pr:
            pic_qs.append(n)
        if "도표" in pr or "그래프" in pr:
            graph_qs.append(n)
    # Image assignment by PDF page + white-ratio filter (background strips are >97% white)
    image_map = {}  # qnum -> list of (src, new_name)
    try:
        from PIL import Image as _PILImage
        def white_ratio(path):
            try:
                im = _PILImage.open(path).convert("L")
                w, h = im.size
                if w < 300:
                    return 0.0
                px = im.load()
                tot = 0; white = 0
                for y in range(0, h, 10):
                    for x in range(0, w, 10):
                        tot += 1
                        if px[x, y] > 240:
                            white += 1
                return white / tot if tot else 0.0
            except Exception:
                return 0.0
    except Exception:
        white_ratio = lambda path: 0.0
    # pdfimages page mapping
    page_for_img = {}
    try:
        import subprocess, json
        raw_pdf = Path(problem_pdf)
        if raw_pdf.exists():
            out = subprocess.run(["pdfimages", "-list", str(raw_pdf)], capture_output=True, text=True, timeout=30).stdout
            for line in out.splitlines():
                m = re.match(r"\s*(\d+)\s+(\d+)\s+", line)
                if m:
                    pg, num = int(m.group(1)), int(m.group(2))
                    page_for_img[num] = pg
    except Exception as e:
        log.append(f"{stem}: pdfimages failed {e}")
    # pages from problem_cols: split by form feed
    pages_qs = {}
    try:
        chunks = prob_text.split("\f")
        # chunks are per column: page p -> chunks 2p-2 (left) and 2p-1 (right), with possible extra empty chunks
        # Build by scanning chunks in order and grouping pairs, but also handle  at end of each column text
        # Simpler: iterate chunks, assign chunk index //2 +1 as page (since 2 columns per page)
        for ci, ptxt in enumerate(chunks):
            pg = ci // 2 + 1
            for ln in ptxt.splitlines():
                hh = q_header(ln)
                if hh and hh not in pages_qs.setdefault(pg, []):
                    pages_qs[pg].append(hh[0])
    except Exception:
        pages_qs = {}
    # content images in extraction order
    content_imgs = []
    for src in img_files:
        wr = white_ratio(src)
        if wr < 0.97:
            content_imgs.append(src)
        else:
            log.append(f"{stem}: discarded background strip {src.name} white={wr:.2f}")
    # assign: for each content image, find its page, then target question on that page
    # target priority: pic/graph question on that page, else notice question on that page (prompt contains 안내문/Contest/Workshop etc in full prompt), else first question on page
    # group consecutive images on same page to same target if they are strips of same figure (same width approx, or pic/graph page)
    assigned_counts = {}
    for idx, src in enumerate(content_imgs):
        # orig index in img_files (kordoc order = pdfimages num order)
        orig_idx = img_files.index(src) if src in img_files else idx
        pg = page_for_img.get(orig_idx)
        qs_page = pages_qs.get(pg, []) if pg else []
        target = None
        # image width
        try:
            from PIL import Image as _IM2
            img_w = _IM2.open(src).size[0]
        except Exception:
            img_w = 1500
        if qs_page:
            pic_graph_on_page = [qn for qn in qs_page if qn in pic_qs or qn in graph_qs]
            notice_on_page = [qn for qn in qs_page if qn in questions and ("안내문" in full_prompt(questions[qn]) or "에 관한" in full_prompt(questions[qn]))]
            if img_w >= 1000 and pic_graph_on_page:
                target = pic_graph_on_page[0]
            elif img_w < 1000 and notice_on_page:
                target = notice_on_page[0]
            elif pic_graph_on_page:
                target = pic_graph_on_page[0]
            elif notice_on_page:
                target = notice_on_page[0]
            if target is None:
                # if previous image on same page was assigned, reuse same target (strips)
                if idx > 0 and page_for_img.get(img_files.index(content_imgs[idx-1]) if content_imgs[idx-1] in img_files else idx-1) == pg and content_imgs[idx-1] in [s for lst in image_map.values() for s,_ in lst]:
                    # find target of previous
                    for qn, lst in image_map.items():
                        if lst and lst[-1][0] == content_imgs[idx-1]:
                            target = qn; break
            if target is None:
                log.append(f"{stem}: content image {src.name} on page {pg} unassigned (qs {qs_page})")
                continue
        else:
            log.append(f"{stem}: content image {src.name} page unknown/unmapped")
            continue
        cnt = assigned_counts.get(target, 0) + 1
        assigned_counts[target] = cnt
        suffix = "" if cnt == 1 else str(cnt)
        new_name = f"{year}_M{month}_{grade}_{target:02d}_image{suffix}.jpg"
        image_map.setdefault(target, []).append((src, new_name))
    for qn in pic_qs + graph_qs:
        if qn not in image_map:
            log.append(f"{stem}: Q{qn} needs image but no content image assigned")
    # Group-level info: order letters and grammar corrections for shared passages
    group_info = {}
    try:
        for (s,e), plines in groups.items():
            gi = {}
            # find order question in group
            for qn in range(s, e+1):
                if qn not in questions:
                    continue
                # full prompt for that q (first para + group prompt)
                qobj = questions[qn]
                fl = ([qobj["prompt"]] if qobj["prompt"] else []) + qobj["body_lines"]
                fp_lines = []
                for ln in fl:
                    if not ln.strip():
                        break
                    fp_lines.append(ln.strip())
                fp = " ".join(fp_lines)
                gp_txt = groups_prompt.get((s,e), "")
                prompt_q = fp if fp else gp_txt
                if "순서" in prompt_q and ("배열" in prompt_q or "이어질" in prompt_q):
                    # parse its options: use body paras options
                    # reconstruct paras from its block
                    # Use extract_options on body_lines (old helper) then parse
                    opts_raw, _ = extract_options(qobj["body_lines"])
                    # also include first para options if merged? use parse on opts_raw joined
                    od = parse_options_text(opts_raw) if opts_raw else {}
                    if answer_key and qn in answer_key and od and answer_key[qn] in od:
                        letters = re.findall(r"\(([A-D])\)", od[answer_key[qn]])
                        if letters:
                            gi["order"] = [l.strip("()") for l in letters]
                            gi["order_q"] = qn
                # grammar corrections: marked words in group passage
                gp_text_all = "\n\n".join(join_chunks(plines))
                marked = re.findall(r"\([a-e]\)\s*([A-Za-z\-']+)", gp_text_all)
                if marked:
                    for (w,c) in arrow_pairs:
                        if w in marked:
                            gi.setdefault("corrections", []).append((w,c))
            if gi:
                group_info[(s,e)] = gi
    except Exception as ex:
        log.append(f"{stem}: group_info failed {ex}")
    # Markdown option tables from the kordoc problem parse (Q10/Q12).
    md_tables = {}
    prob_md_path = workdir / "problem.md"
    if prob_md_path.exists():
        try:
            md_tables = extract_md_tables(prob_md_path.read_text(encoding="utf-8", errors="ignore"))
        except Exception as e:
            log.append(f"{stem}: md table extraction failed {e}")
    # Build output
    out_lines = []
    for n in range(1,46):
        if n not in questions:
            out_lines.append(f"{n}. [missing in parsed text]")
            out_lines.append("")
            continue
        q = questions[n]
        prompt, passage, opts = apply_restoration(n, q, groups, groups_prompt, answer_key, arrow_pairs, log, stem, group_info=group_info, md_tables=md_tables, appropriate_words=appropriate_words)
        # "[3점]" belongs to the prompt: when it sits in the raw block
        # (e.g. glued to the last passage line under a bare "37."
        # header) but not in the prompt, move it there. The passage
        # side is stripped in the final clean below.
        if "[3점]" not in prompt and "[3점]" in " ".join(q["body_lines"]):
            prompt = prompt + " [3점]"
        # Final clean: ____ placeholders from column gaps should not appear in output (fill success already replaced them)
        passage = passage.replace("____", " ").replace("[3점]", " ") if passage else passage
        if passage:
            passage = normalize_passage_text(passage).strip()
        prompt = prompt.replace("____", " ").strip() if prompt else prompt
        if opts:
            opts = [o.replace("____", " ").strip() for o in opts]
            # A picture-item label ("② ____") becomes a bare marker once
            # the gap marker is removed; bare markers carry no content.
            opts = [o for o in opts if o and not re.fullmatch(r"[①②③④⑤\s]+", o)]
        # Korean prompt word-split fix: join known prompt words split across lines
        prompt = join_prompt_words(prompt)
        prompt = re.sub(r"\s+", " ", prompt).strip()
        # Column joins leave spaces before punctuation in prompts too
        # ("듣고 ,", "고르시오 ."); same fix as for passages.
        prompt = strip_space_before_punct(prompt)
        # Restored-format prompt normalizations (verified across all 74
        # baseline files): the (a)-(e) range becomes "낱말", and the (A)
        # lead reference drops out of Q43 prompts.
        prompt = re.sub(r"밑줄 친\s*\(a\)\s*[~～∼\-－]\s*\(e\)\s*중에서", "밑줄 친 낱말 중에서", prompt)
        prompt = re.sub(r"주어진 글\s*\(A\)에 이어질", "주어진 글에 이어질", prompt)
        out_lines.append(f"{n}. {prompt}")
        out_lines.append("")
        # For 1-17, passage may be table/image area; if passage empty and no opts, log?
        if n <= 17:
            # A bare speaker label ("Man:") sits directly above the
            # options with no blank line, as in the restored format.
            _bare_label = bool(passage) and len(passage.strip()) <= 40 and re.fullmatch(r"[A-Za-z][A-Za-z .'-]*:", passage.strip())
            if passage:
                out_lines.append(passage)
                if not _bare_label:
                    out_lines.append("")
            if opts:
                # table options: if opts contain '|' keep as table lines
                for o in opts:
                    out_lines.append(o)
                out_lines.append("")
            # image for this question?
            if n in image_map:
                for src, new_name in image_map[n]:
                    out_lines.append(f"![image]({new_name})")
                    out_lines.append("")
        else:
            # 18-45: passage only, plus image/table for graph/picture (picture should not occur in 18-45 except graph)
            if n in image_map:
                # For graph, include image + OCR table placeholder
                paras = passage.split("\n\n") if passage else []
                if paras:
                    out_lines.append(paras[0])
                    out_lines.append("")
                    for src, new_name in image_map[n]:
                        out_lines.append(f"![image]({new_name})")
                        out_lines.append("")
                    for pp in paras[1:]:
                        out_lines.append(pp)
                        out_lines.append("")
                    if n in graph_qs:
                        log.append(f"{stem}: Q{n} graph table needs manual/OCR verification (image included, table to be added in post-step)")
                else:
                    for src, new_name in image_map[n]:
                        out_lines.append(f"![image]({new_name})")
                        out_lines.append("")
                    if n in graph_qs:
                        log.append(f"{stem}: Q{n} graph table needs manual/OCR verification (image included, table to be added in post-step)")
                # skip old block below by using a flag: we already output, so continue to next question via a trick - set a variable
                # Actually old code follows; to avoid duplication, we will handle by jumping: the following if/else is removed by replacement below
                if True:
                    continue
            else:
                if passage:
                    out_lines.append(passage)
                    out_lines.append("")
                else:
                    log.append(f"{stem}: Q{n} empty passage")
    text = "\n".join(out_lines).strip() + "\n"
    # Post-process: remove any remaining dash before circled in output (should not happen)
    text = re.sub(r"(?m)^\s*-\s+([①②③④⑤])", r"\1", text)
    return text, image_map, log

def main():
    import argparse
    ap = argparse.ArgumentParser(description="Build restored exam markdown from prepared parses.")
    ap.add_argument("--repo-root", default=None,
                    help="repository root (default: parent of scripts/)")
    ap.add_argument("--raw-dir", default=None,
                    help="raw PDF directory (default: <repo-root>/data/raw)")
    ap.add_argument("--work-dir", default=None,
                    help="prepared-parse work directory (default: <repo-root>/.restore-work)")
    ap.add_argument("--output-dir", default=None,
                    help="restored output directory (default: <repo-root>/data/output)")
    ap.add_argument("--only", nargs="*", default=None, help="stems to build, e.g. 2026_03_H3")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="write only to a temporary directory, never to --output-dir")
    ap.add_argument("--dry-run-dir", default=None,
                    help="dry-run output directory (default: fresh mkdtemp)")
    ap.add_argument("--force", action="store_true",
                    help="overwrite existing restored files (default: skip them)")
    args = ap.parse_args()

    repo_root = Path(args.repo_root) if args.repo_root else default_repo_root()
    raw_dir = Path(args.raw_dir) if args.raw_dir else repo_root / "data" / "raw"
    work_dir = Path(args.work_dir) if args.work_dir else repo_root / ".restore-work"
    out_dir_base = Path(args.output_dir) if args.output_dir else repo_root / "data" / "output"

    stems_map = discover_stems(raw_dir)
    stems = sorted(stems_map)
    if args.only:
        stems = [s for s in stems if s in args.only]
    if args.list:
        print("\n".join(stems))
        return

    if args.dry_run:
        if args.dry_run_dir:
            dry_dir = Path(args.dry_run_dir)
            dry_dir.mkdir(parents=True, exist_ok=True)
        else:
            dry_dir = Path(tempfile.mkdtemp(prefix="csat-restore-dryrun-"))
        out_base = dry_dir
        print(f"Dry run: outputs go to {dry_dir} (real output directory untouched)")
    else:
        out_base = out_dir_base

    work_dir.mkdir(parents=True, exist_ok=True)
    log = []
    results = {}
    for stem in stems:
        year_s, month_s, grade_s = stem.split("_")
        grade_num = grade_s[1]  # 1,2,3
        out_dir = out_base / f"고{grade_num}"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_name = f"{year_s}_M{int(month_s)}_{grade_s}_restored.md"
        out_path = out_dir / out_name
        if not args.dry_run and out_path.exists() and not args.force:
            results[stem] = "skipped_existing"
            log.append(f"{stem}: output exists, skipped (use --force to overwrite)")
            continue
        text, image_map, log = build_exam(stem, log, stems_map[stem], work_dir)
        if text is None:
            results[stem] = "failed"
            continue
        out_path.write_text(text, encoding="utf-8")
        # copy images as JPG
        from PIL import Image
        for qn, lst in image_map.items():
          for src, new_name in lst:
            dst = out_dir / new_name
            try:
                im = Image.open(src).convert("RGB")
                # white background for transparency
                bg = Image.new("RGB", im.size, (255,255,255))
                if im.mode in ("RGBA","LA"):
                    bg.paste(im, mask=im.split()[-1])
                    im = bg
                im.save(dst, "JPEG", quality=92)
            except Exception as e:
                log.append(f"{stem}: image convert failed Q{qn} {src} {e}")
                # fallback copy
                import shutil; shutil.copy(src, dst)
        results[stem] = "ok"
    (work_dir / "restoration_log.json").write_text(json.dumps({"log": log, "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    built = sum(1 for v in results.values() if v == "ok")
    skipped = sum(1 for v in results.values() if v == "skipped_existing")
    print(f"Built {built}/{len(stems)}" + (f" (skipped existing: {skipped})" if skipped else ""))
    if args.dry_run:
        print(f"Dry-run output directory: {dry_dir}")
    print(f"Log entries: {len(log)}")
    for entry in log:
        print(" -", entry)

if __name__ == "__main__":
    main()
