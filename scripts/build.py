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
        s = raw.rstrip()
        if not s.strip():
            lines.append("")
            continue
        # horizontal separator lines
        if set(s.strip()) <= set("━─═-=") and len(s.strip()) >= 10:
            continue
        # large leading indent (>=20 spaces) indicates a blank placeholder, except footer/token/footnote/[3점] lines (those are handled below)
        _lead = len(raw) - len(raw.lstrip(" "))
        if _lead >= 20 and not (s.strip() in ("영어","영역","고1","고2","고3") or s.strip().isdigit() or s.strip().startswith("*") or s.strip() == "[3점]"):
            lines.append("____ " + s.strip())
            continue
        # headers/footers: any line consisting only of 영어/영역/고N/digits
        if re.fullmatch(r"\s*(영어|영역|고[123]|\d+)(\s+(영어|영역|고[123]|\d+))*\s*", s):
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
        if re.match(r"\s*\*\s", s) and re.search(r"[가-힣]", s):
            # vocabulary footnote line, e.g. "* embed: 파묻다" - remove, as in reference
            continue
        lines.append(s)
    return lines

def is_group_heading(line):
    m = re.search(r"\[(\d+)\s*[~～∼-]\s*(\d+)\]\s*(.*)", line)
    return m

def q_header(line):
    # Require space after dot (or end of line for bare headers) to avoid decimal numbers like 42.6% in graphs
    m = re.match(r"\s*(\d{1,2})\.(?:\s+(.*)|\s*)$", line)
    if m and 1 <= int(m.group(1)) <= 45:
        return int(m.group(1)), (m.group(2).strip() if m.group(2) else "")
    return None

def extract_answer_key(ans_text):
    """Return dict 1..45 -> circled int, or None if incomplete."""
    key = {}
    for line in ans_text.splitlines():
        # lines like " 1 ① 2 ⑤ 3 ① ..." possibly with trailing explanation text
        pairs = re.findall(r"(\d{1,2})\s+([①②③④⑤])", line)
        # only accept lines that start (after spaces) with a number and contain >=2 pairs, and are near 정답? 
        # To avoid picking up question options, require line to have at least 3 pairs and no Korean explanation before first pair
        if len(pairs) >= 3:
            # check the line does not look like a question option line (options are single)
            for num, circ in pairs:
                n = int(num)
                if 1 <= n <= 45 and n not in key:
                    key[n] = CIRC_MAP[circ]
    if len(key) == 45:
        return key
    return key if key else None

def extract_arrow_corrections(ans_text):
    pairs = []
    for m in re.finditer(r"([A-Za-z][A-Za-z\-']+)\s*→\s*([A-Za-z]+(?: [A-Za-z]+){0,2})", ans_text):
        w, c = m.group(1).strip(), m.group(2).strip()
        # trim trailing common words that are not part of phrase? keep as is, but strip if c ends with '으로' etc - not in English pattern
        if w.lower() != c.lower():
            pairs.append((w, c))
    return pairs

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

def split_questions(lines):
    """
    Return ordered dict: qnum -> {'prompt': str, 'body_lines': [], 'group_passage': str or None, 'group_range': tuple or None}
    and group_passages: {(start,end): passage_text_raw}
    Strategy: walk lines, when group heading found, collect following lines as group passage
    until first question number in range. Then questions in range share that passage.
    For non-group questions, body is lines until next question/group heading.
    """
    questions = {}
    groups = {}  # (start,end) -> raw lines
    groups_prompt = {}
    cur_group = None
    cur_q = None
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
            # avoid duplicate headers from footers / answer table etc: if already seen and this is a later duplicate with same number, keep first
            if n in blocks:
                # duplicate: append as body to current? skip header line
                i += 1
                continue
            blocks[n] = [prompt]
            order.append(n)
            cur_q = n
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

def apply_restoration(qnum, q, groups, groups_prompt, answer_key, arrow_pairs, log, exam_id, group_info=None):
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
    # Options from body paras
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
                parts = re.split(r"\(([A-D])\)", gp_text)
                seg = {}
                intro = parts[0].strip() if parts else ""
                for idx in range(1, len(parts)-1, 2):
                    seg[parts[idx]] = parts[idx+1].strip()
                letters = gi["order"]
                if set(seg.keys()) >= set(letters):
                    ordered = []
                    if intro:
                        ordered.append(intro)
                    for L in letters:
                        if L in seg:
                            ordered.append(seg[L])
                    group_passage_paras = ["\n\n".join(ordered)]
            # corrections: apply to non-grammar questions in group (grammar q handles itself)
            if not _is_grammar:
                for (w,c) in gi.get("corrections", []):
                    group_passage_paras = [re.sub(rf"\b{re.escape(w)}\b", c, pp) for pp in group_passage_paras]
    body_paras = non_opt_paras
    # For 1-17: keep options, passage is non-option paras (usually empty, except table/image cases handled by caller)
    if qnum <= 17:
        # opts may be multi-option paras: split into individual options
        clean_opts = []
        for o in opts:
            # split by circled
            parts = re.split(r"([①②③④⑤])", o)
            for idx in range(1, len(parts)-1, 2):
                txt = parts[idx+1].strip() if idx+1 < len(parts) else ""
                if txt:
                    clean_opts.append(f"{parts[idx]} {txt}")
                else:
                    clean_opts.append(parts[idx])
        # table options: if opts contain |, keep as table lines
        passage = "\n\n".join(body_paras)
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
    is_order = ("순서" in prompt_lower and ("배열" in prompt_lower or "이어질" in prompt_lower or "다음에 이어질" in prompt_lower))
    is_insertion = "들어가기에 가장 적절한 곳" in prompt_lower or "주어진 문장이 들어가" in prompt_lower
    is_fill = "빈칸에 들어갈 말" in prompt_lower or "빈칸" in prompt_lower
    is_grammar_vocab = ("어법상 틀린" in prompt_lower or "문맥상 낱말" in prompt_lower) and ("가리키는" not in prompt_lower) and ("의미하는" not in prompt_lower)
    # --- Order ---
    if is_order and answer_key and qnum in answer_key:
        ans = answer_key[qnum]
        if opt_dict and ans in opt_dict:
            opt_text = opt_dict[ans]
            letters = re.findall(r"\([A-D]\)", opt_text)
            if letters and len(letters) >= 2:
                raw_gp = passage
                parts = re.split(r"\(([A-D])\)", raw_gp)
                seg = {}
                intro = parts[0].strip() if parts else ""
                for idx in range(1, len(parts)-1, 2):
                    seg[parts[idx]] = parts[idx+1].strip()
                if set(seg.keys()) >= set([l.strip("()") for l in letters]):
                    ordered = []
                    if intro:
                        ordered.append(intro)
                    for l in letters:
                        kl = l.strip("()")
                        if kl in seg:
                            ordered.append(seg[kl])
                    passage2 = "\n\n".join(ordered)
                    passage2 = strip_markers(passage2)
                    return prompt, passage2, None
                else:
                    log.append(f"{exam_id} Q{qnum}: order segments not found, labels={list(seg.keys())}, expected={letters}")
            else:
                log.append(f"{exam_id} Q{qnum}: order option text not parsed: {opt_text}")
        else:
            log.append(f"{exam_id} Q{qnum}: order answer/options missing")
    # --- Insertion ---
    if is_insertion and answer_key and qnum in answer_key:
        ans = answer_key[qnum]
        # body_paras: first para is given sentence, rest is main passage
        if len(body_paras) >= 2:
            given = body_paras[0]
            main = "\n\n".join(body_paras[1:])
            main = re.sub(r"\(\s*([①②③④⑤])\s*\)", r"\1", main)
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
                fill_text = opt_dict[ans].strip()
                if "____" in passage:
                    passage = passage.replace("____", fill_text, 1)
                elif re.search(r"\s+\.", passage):
                    passage = re.sub(r"\s+\.", f" {fill_text}.", passage, count=1)
                else:
                    log.append(f"{exam_id} Q{qnum}: fill blank marker not found, fill='{fill_text}', passage_end='{passage[-80:]}'")
                passage = strip_markers(passage)
                return prompt, passage, None
            else:
                log.append(f"{exam_id} Q{qnum}: fill option missing for answer {ans}")
    # --- Summary fill (40-type) ---
    if "요약" in prompt_lower:
        pair_opts = {}
        for num, txt in (opt_dict or {}).items():
            parts = re.split(r"[·…]+", txt)
            parts = [p.strip() for p in parts if p.strip()]
            if len(parts) >= 2:
                pair_opts[num] = (parts[0], parts[-1])
            elif len(parts) == 1:
                # fallback: split by multiple spaces
                sp = re.split(r"\s{2,}", txt.strip())
                sp = [s.strip() for s in sp if s.strip()]
                if len(sp) >= 2:
                    pair_opts[num] = (sp[0], sp[-1])
        if answer_key and qnum in answer_key and pair_opts and answer_key[qnum] in pair_opts:
            a_word, b_word = pair_opts[answer_key[qnum]]
            passage2 = passage
            passage2 = passage2.replace("(A)", a_word).replace("(B)", b_word)
            passage2 = strip_markers(passage2)
            return prompt, passage2, None
        else:
            log.append(f"{exam_id} Q{qnum}: summary fill not applied, pair_opts={pair_opts}, answer={answer_key.get(qnum) if answer_key else None}")
    # --- Grammar / vocab correction (per-question arrow pairs if available, else global) ---
    if is_grammar_vocab:
        pairs_to_use = arrow_pairs
        # arrow_pairs may be a dict {qnum: [(w,c),...]} if caller passes per-question, else list
        if isinstance(arrow_pairs, dict):
            pairs_to_use = arrow_pairs.get(qnum, [])
        marked = re.findall(r"[①②③④⑤]\s*([A-Za-z\-']+)", passage) + re.findall(r"\([a-e]\)\s*([A-Za-z\-']+)", passage)
        if marked:
            matches = [(w,c) for w,c in pairs_to_use if w in marked]
        else:
            matches = [(w,c) for w,c in pairs_to_use if re.search(rf"\b{re.escape(w)}\b", passage)]
        if len(matches) == 1:
            w,c = matches[0]
            passage = re.sub(rf"\b{re.escape(w)}\b", c, passage, count=1)
            passage = strip_markers(passage)
            return prompt, passage, None
        elif len(matches) > 1:
            log.append(f"{exam_id} Q{qnum}: grammar/vocab ambiguous matches {matches}, not applied")
        else:
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
    questions, groups, groups_prompt = split_questions(lines)
    # answer key
    answer_key = None
    arrow_pairs = []
    answer_type = "missing"
    if ans_cols_path.exists():
        ans_text = ans_cols_path.read_text(encoding="utf-8", errors="ignore")
        answer_key = extract_answer_key(ans_text)
        # arrows: prefer kordoc answer.md, else full-width answer_full.txt (both avoid column truncation)
        ans_md_path = workdir / "answer.md"
        ans_full_path = workdir / "answer_full.txt"
        if ans_md_path.exists():
            arrow_src = ans_md_path.read_text(encoding="utf-8", errors="ignore")
        elif ans_full_path.exists():
            arrow_src = ans_full_path.read_text(encoding="utf-8", errors="ignore")
        else:
            arrow_src = ans_text
        arrow_pairs = extract_arrow_corrections(arrow_src)
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
    # Build output
    out_lines = []
    for n in range(1,46):
        if n not in questions:
            out_lines.append(f"{n}. [missing in parsed text]")
            out_lines.append("")
            continue
        q = questions[n]
        prompt, passage, opts = apply_restoration(n, q, groups, groups_prompt, answer_key, arrow_pairs, log, stem, group_info=group_info)
        # Final clean: ____ placeholders from column gaps should not appear in output (fill success already replaced them)
        passage = passage.replace("____", " ").replace("  ", " ") if passage else passage
        prompt = prompt.replace("____", " ").strip() if prompt else prompt
        if opts:
            opts = [o.replace("____", " ").strip() for o in opts]
        # Korean prompt word-split fix: join known prompt words split across lines
        _known = ["고르시오","적절한","가장","일치하지","일치하는","내용","제목","주제","요지","목적","심경","변화","주장","드러난","의미하는","관계","없는","문장","순서","배열","빈칸","들어갈","요약","하고자","다음","글의","중에서","밑줄","부분","어법상","틀린","문맥상","낱말","쓰임","적절하지","않은","것은","가리키는","대상","나머지","다른","주어진","이어질","맞게","배열한","것으로","관한","안내문의","일치하는","상황","설명","듣고","대화를","여자가","남자가","할","일로","이유를"]
        for _w in _known:
            for _i in range(1, len(_w)):
                prompt = prompt.replace(_w[:_i] + " " + _w[_i:], _w)
        prompt = re.sub(r"\s+", " ", prompt).strip()
        out_lines.append(f"{n}. {prompt}")
        out_lines.append("")
        # For 1-17, passage may be table/image area; if passage empty and no opts, log?
        if n <= 17:
            if passage:
                out_lines.append(passage)
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
    for entry in log[:200]:
        print(" -", entry)

if __name__ == "__main__":
    main()
