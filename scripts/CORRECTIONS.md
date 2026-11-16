# Phase 3: correction dictionary, corrections.json, queue, graph tables

## Files

| File | Contents |
| --- | --- |
| `correction_dictionary.json` | 109 auto-apply corrections (61 audit-confirmed + 48 evidenced in phase 3) |
| `corrections.json` | 67 exceptions (62 audit-confirmed + 5 judgment calls), post-assembly safety net |
| `correction_queue.json` | 12 items that must never be auto-applied |
| `graph_tables.py` | Strict Q25 data-table extraction from the kordoc problem parse |
| `build.py` | Wiring: dictionary-first application, safety net, queue reports |

## Precedence

1. The dictionary applies first, inside `apply_restoration`, with strict
   targeting only (see below). A dictionary entry supersedes extraction
   for its item.
2. Extraction (arrow pairs, appropriate-word fallback) runs when no
   dictionary entry applies, or when dictionary targeting fails.
3. `corrections.json` applies after assembly. **On any conflict,
   `corrections.json` wins**, because it runs last. In practice the two
   files agree on every shared entry (verified by the builder); the rule
   exists so a disagreement resolves deterministically.
4. Queue items are never applied by any path. The original text stays.

## Targeting rules (no guessing, no over-application)

A correction is applied only at one of these sites, in order:

1. The marked span at the answer-key position (the ①–⑤ / (a)–(e)
   underline holding the old word at the key's number).
2. The unique marked span holding the old word.
3. The evidence window: a word sequence from the audit evidence that
   occurs exactly once in the item (punctuation- and hyphen-tolerant)
   with the old word inside it.

There is no first-occurrence or global fallback anywhere on the
dictionary or corrections paths. The legacy `_replace_at_marked`
first-occurrence fallback remains only on the extraction path, which
predates phase 3.

The safety net adds one more guard: when the new word is already
present in the item and no evidence window pins a site, the entry is
skipped. Remaining old-word occurrences in that case are legitimate
unmarked ones. This is the rule that blocks the over-application class
found in the audit fix pass (2020_M6_H2 Q29 sentence-1 `that`,
2024_M6_H2 Q29 first `smiling`); both are regression-checked on every
rebuild (see below).

Insertions (`kind: "insert"`) splice before the anchor word's marked
span. Only insertions with supporting explanation evidence are in the
dictionary (2022_M10_H3 Q29 `was`, 2024_M10_H2 Q29 `been`).

## Entry schemas

Dictionary entry:

```json
{
  "stem": "2020_M3_H1",
  "q": 29,
  "applies_to": [29],
  "old": "which",
  "new": "that",
  "kind": "replace",
  "grade": "P3",
  "evidence_kind": "swap",
  "evidence": "해설 swap: which를 that으로 바꿔야 한다",
  "evidence_ref": "phase-3 evidence review 2026-11-16",
  "window": "…"
}
```

- `stem` uses the restored-file form (`YYYY_M<month>_H<grade>`, month
  not zero-padded). `q` is the primary item; `applies_to` lists every
  item sharing the passage (Q41/Q42 groups list both).
- `grade` is the audit grade (`E`/`R`/`G`) when the entry comes from
  the audit, or `P3` when it was ungraded at audit time and evidenced
  during phase 3. `evidence_kind` names the evidence shape: `arrow`,
  `swap`, `gloss`, `explicit`, `official` (2026_M9 official solutions),
  `pua-anchor` (2021_M11_H2, verified PUA key decode), `readme7`,
  `audit`.
- `window` is optional. When present it was verified to occur exactly
  once (or once with old→new swapped) in the stage-2 rebuild item.

`corrections.json` entries use the same shape plus `"apply": true` /
`false`. Exactly one entry has `"apply": false`: 2021_M11_H1 Q29
(`occurring` → `occur`), held in the queue because build.py cannot read
its answer key (see PUA note below). The 5 `J` (judgment) entries are
corrections.json-only: they are applied by the safety net, not by the
dictionary.

Queue definitions use:

```json
{
  "stem": "2020_M11_H1",
  "q": 42,
  "applies_to": [41, 42],
  "kind": "correction-uncertain",
  "grade": "G?",
  "old": "crucial",
  "candidate_new": "secondary/incidental",
  "candidate_status": "unconfirmed",
  "reason": "…",
  "evidence_ref": "grades.json:2020_M11_H1_Q42"
}
```

`candidate_new` is never applied. Queue kinds: `correction-uncertain`
(G?, 7), `correction-image-only` (IMG, 4), `correction-key-blocked`
(E, 1). The per-run queue report adds `correction-targeting-failed`,
`image-unassigned`, `image-missing`, and `graph-table-missing`.

## PUA answer-grid note (2021_M11_H1/H2)

The 2021_11 answer grids use PUA glyphs U+E9BE–U+E9C2 with no
`/ToUnicode`, so no extractor reads the keys. A position-based
investigation (2026-11-16) verified the sequential mapping E9BE=1 …
E9C2=5 with 20 in-target anchors and 90/90 cross-file calibration
points. The decode is recorded in the queue entry for 2021_M11_H1 Q29
(decoded key: 4) but is NOT wired into build.py; promoting that entry
needs a key-decode step in prepare.py. The 2021_M11_H2 corrections in
the dictionary rest on the same verified decode plus explanation
glosses, and apply through marked spans without needing the key at
build time.

## Graph tables

`graph_tables.py` extracts Q25 data tables only from the kordoc
problem parse (the PDF text layer). A table is emitted only when it
parses completely: consistent columns, textual headers, labelled data
rows, at least two data rows. Side-by-side tables split on all-empty
spacer columns. Titles come from the English lines directly above the
table; clipped fragments are dropped. Notes directly below a table are
kept.

Rejected cases (bar-chart axis junk, fragmented cells, chart images
with no text table) stay image-only and are queued. No values are ever
estimated. Four exams cannot have tables at all (README §2):
2021_M3_H2, 2022_M3_H2, 2021_M3_H3, 2021_M7_H3.

Current yield on the 74-exam corpus: 6 tables emitted (3 exactly
matching the baseline tables, 3 with identical data but different
header wording or split structure), 68 queued.

## Images

`prepare.py` writes pdfimages output to `images/pdfimages/` (true PDF
order). `build.py` prefers that directory for page mapping and falls
back to kordoc's `images/problem/` when it is absent. Unassigned
content images and picture/graph questions without images are queued,
never assigned arbitrarily.

## Regression checks (run after every rebuild)

1. 2020_M6_H2 Q29: sentence-1 `that` untouched; only the ⑤ `that`
   becomes `what`.
2. 2024_M6_H2 Q29: the first `smiling` untouched; only the ⑤ `smiling`
   becomes `smile`.
3. 2021_M11_H1 Q29 keeps `occurring` (queued, key-blocked).
4. Every dictionary/corrections application verified word-by-word
   against its evidence (triage `CORRECTION`/`QUEUED` review).
