#!/usr/bin/env python3
"""Hyphen repair for kordoc problem parses (CSAT restoration pipeline).

kordoc drops hyphens in compound words ("face-to-face" -> "facetoface")
and sometimes spaces them ("real - world"). The question PDF's own text
layer carries the correct form as a soft hyphen (U+00AD), so the repair
dictionary is derived per exam from the PDF itself -- no per-file
hand-written dictionary is needed.

Safety rules (these are what make the module safe to run blindly):
- Only words whose soft hyphen sits strictly INSIDE the token are
  accepted. A soft hyphen at a token edge is a line-break fragment
  ("him-" from "him-self" split across lines), not a compound word;
  accepting it would rewrite every bare "him" in the parse.
- Ambiguous words are skipped: when the PDF prints BOTH the hyphenated
  form ("on-line" in one question) and the bare form ("online" in
  another), the bare form may be the exam's own printed text, so the
  word is left alone and logged instead of rewritten.
- Only the wrong forms (joined, spaced) of dictionary words are
  replaced, on word boundaries; words outside the dictionary are
  never touched. The replacement is idempotent: a second run is a
  no-op because the correct forms contain hyphens, not wrong forms.
- A missing pdftotext binary, a missing PDF, or a pdftotext failure
  only skips the fix (reported in the returned stats); it never raises
  and never fails the build.

Usage from build.py (right after problem.md is loaded):
    from hyphen_fix import fix_md_hyphens
    prob_md_text, stats = fix_md_hyphens(prob_md_text, problem_pdf)
    if stats.get("skipped"):
        log.append(f"{stem}: hyphen fix skipped ({stats['reason']})")
"""
import re
import shutil
import subprocess
from pathlib import Path

SOFT = "\u00ad"
# Punctuation that may surround a word. A word's own apostrophe (Merleau-Ponty's)
# is never stripped.
LEAD_PUNCT = "\u201c\"'\u2018([{"
TRAIL_PUNCT = "\u201d\"'\u2019.,;:!?)]"


class HyphenFixSkipped(Exception):
    """Raised when the fix cannot run (no pdftotext, no PDF, ...).

    fix_md_hyphens() converts this into a stats dict; direct callers of
    extract_pdf_text() may let it propagate.
    """


def extract_pdf_text(pdf_path):
    """Full text of a PDF via `pdftotext -layout` (stdout).

    Raises HyphenFixSkipped when pdftotext is missing, the PDF is
    missing, or pdftotext fails.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise HyphenFixSkipped(f"PDF not found: {pdf_path}")
    if shutil.which("pdftotext") is None:
        raise HyphenFixSkipped("pdftotext not found on PATH")
    try:
        out = subprocess.run(
            ["pdftotext", "-layout", str(pdf_path), "-"],
            capture_output=True, text=True, check=True,
        ).stdout
    except (subprocess.CalledProcessError, OSError) as e:
        raise HyphenFixSkipped(f"pdftotext failed for {pdf_path}: {e}")
    return out


def extract_hyphen_words(pdf_text):
    """Soft-hyphen compound words in PDF text -> correct hyphenated form.

    Only tokens whose soft hyphen sits strictly inside the word are
    accepted; edge fragments (line-break splits) are dropped. Returns
    {correct_form: occurrence_count}.
    """
    found = {}
    for token in re.findall(r"\S*" + re.escape(SOFT) + r"\S*", pdf_text):
        word = token.strip(LEAD_PUNCT).rstrip(TRAIL_PUNCT)
        if SOFT not in word:
            continue
        correct = word.replace(SOFT, "-")
        # A soft hyphen at the word edge is a line-break fragment, not
        # a compound word ("him-" from "him-self"). A doubled hyphen
        # is a malformed token. Drop both.
        if correct.startswith("-") or correct.endswith("-") or "--" in correct:
            continue
        if "-" not in correct:
            continue
        found[correct] = found.get(correct, 0) + 1
    return found


def wrong_forms(correct):
    """The wrong forms kordoc produces for a hyphenated word.

    Joined ("facetoface") and spaced ("real - world"). The spaced form
    only differs when the word actually contains a hyphen.
    """
    joined = correct.replace("-", "")
    spaced = correct.replace("-", " - ")
    forms = [joined]
    if spaced != joined:
        forms.append(spaced)
    return forms


def _wrong_pattern(wrong):
    """Word-boundary regex for one wrong form (spaced form tolerates
    any run of whitespace around the hyphen, as kordoc's spacing varies)."""
    if " - " in wrong:
        return re.compile(
            r"\b" + re.escape(wrong).replace(r"\ - ", r"\s+-\s+") + r"\b")
    return re.compile(r"\b" + re.escape(wrong) + r"\b")


def _ambiguous_words(dictionary, pdf_text):
    """Dictionary words the PDF also prints in a wrong form.

    When the exam prints both "on-line" and bare "online", the bare
    occurrences may be the exam's own text, so rewriting them would
    corrupt the parse. Those words are skipped (returned as a set).
    """
    ambiguous = set()
    for correct in dictionary:
        for wrong in wrong_forms(correct):
            if _wrong_pattern(wrong).search(pdf_text):
                ambiguous.add(correct)
                break
    return ambiguous


def fix(text, dictionary):
    """Replace wrong forms of dictionary words with the correct forms.

    Returns (fixed_text, {description: count}). Dictionary-only,
    word-boundary replacement; idempotent.
    """
    counts = {}
    for correct in dictionary:
        for wrong in wrong_forms(correct):
            pattern = _wrong_pattern(wrong)
            text, n = pattern.subn(correct, text)
            if n:
                key = f"{wrong} -> {correct}"
                counts[key] = counts.get(key, 0) + n
    return text, counts


def verify(text, dictionary):
    """Count correct vs remaining-wrong forms of dictionary words."""
    left = 0
    for correct in dictionary:
        for wrong in wrong_forms(correct):
            left += len(_wrong_pattern(wrong).findall(text))
    ok = sum(len(re.findall(r"\b" + re.escape(c) + r"\b", text))
             for c in dictionary)
    return {"correct_forms": ok, "remaining_wrong": left}


def fix_md_hyphens(md_text, pdf_path):
    """Repair kordoc's hyphen loss in a problem parse, from the PDF.

    Returns (fixed_text, stats). Stats always carries "applied" (bool).
    On success: dictionary size, ambiguous skips, and per-form
    replacement counts. On any failure (no pdftotext, no PDF, pdftotext
    error): the input text unchanged with "skipped": True and a reason.
    Never raises for those conditions.
    """
    try:
        pdf_text = extract_pdf_text(pdf_path)
    except HyphenFixSkipped as e:
        return md_text, {"applied": False, "skipped": True, "reason": str(e)}
    dictionary = extract_hyphen_words(pdf_text)
    if not dictionary:
        return md_text, {"applied": False, "skipped": True,
                         "reason": "no soft-hyphen words found in PDF"}
    ambiguous = _ambiguous_words(dictionary, pdf_text)
    usable = {w: n for w, n in dictionary.items() if w not in ambiguous}
    fixed, counts = fix(md_text, usable)
    stats = {
        "applied": True,
        "dictionary_kinds": len(dictionary),
        "dictionary_occurrences": sum(dictionary.values()),
        "ambiguous_skipped": sorted(ambiguous),
        "replacements": counts,
        "replacement_count": sum(counts.values()),
    }
    return fixed, stats
