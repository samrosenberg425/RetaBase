# RetaBase — How the rules work

Every number on the site is produced by deterministic, inspectable rules. No model
decides what ranks first, what a paper's dose was, or whether evidence is strong.
This document is the reference for what those rules actually do, so a reader can
audit or challenge any value.

Source of truth: `retarats_pipeline/curation/` (`extractors.py`, `reliability.py`,
`ranking.py`, `facets.py`) and `scripts/build_curated_database.py`.

---

## 1. Retrieval — which papers enter the corpus

Per-molecule PubMed queries live in `config/SEARCH_RULES.csv` (one or more "lanes"
per molecule, each with a `match_strength`). Broad, ambiguous molecules (e.g.
glutathione, metformin) are **subject-anchored** — `[majr]`/`[ti]` rather than
all-fields — so a paper that merely mentions the molecule in passing, or uses it as
a reagent, doesn't enter. Sparse molecules get deliberately broader lanes
(`scripts/gen_broad_lanes.py`) because their problem is too little literature, not
too much.

Off-topic classes are excluded by rule, not by taste: opinion/infodemiology studies
(social-media or Google-Trends analyses of public perception), and papers where the
molecule appears only as an exclusion criterion or an assay reagent.

## 2. Extraction — dose, route, duration, sample size

All parsed from title + abstract (and, where available, open-access full text).
The hard part is not finding a number; it is refusing the wrong one.

**Dose.** A number followed by a mass/volume/IU unit, optionally with a per-weight
or per-time compound (`10 mg/kg/day`). Guards, each added in response to a real
observed failure:

- **Not a dose:** bare `kg` (that's body weight — `9 kg` came from a BMI string),
  BMI units (`39·9 kg/m²`), and lab concentrations with a volume denominator
  (`7·2 mmol/L`, `140 mg/dL`, `5 mg/mL` are readouts, not doses).
- **Number formats:** middle-dot decimals (`1·25 mg`, used by The Lancet),
  comma thousands (`1,000 mg` — previously mis-parsed as `000 mg`), hyphenated
  units (`250-µg`), and spelled-out units (`500 micrograms`).
- **Frequency is part of the dose.** `500 mg twice daily` is retained in full;
  `500 mg` alone would understate exposure by half. Handles `BID`, `once weekly`,
  `three times daily`, `q12h`, `/day`.
- **Placebo/vehicle doses are dropped** — in "metformin 750 mg or matching placebo",
  the placebo's dose is not the drug's dose.

**Attribution — which drug does this dose belong to?** The main way an extractor
misleads is by reporting a comparator's dose. Rules, in order:

1. If the abstract shows no comparison, extract document-wide (single-drug paper).
2. If it does compare drugs, prefer **adjacency**: a dose immediately following this
   molecule's name is its own — `metformin (750 mg twice daily)` — even when the
   sentence lists three drugs.
3. Adjacency is rejected if a conjunction or list separator intervenes
   (`metformin 500 mg **or** pioglitazone 7.5 mg`, `metformin (200 mg/kg)**,** RCL
   (0.75 g/kg)`), because that hands ownership to another agent.
4. With no adjacency evidence and multiple candidate doses, the field is **left
   blank** and flagged `ambiguous_multidrug`. Blank means "not safely attributable",
   never zero.

Each record records which path was used in `refined_extraction_scope`
(`document` / `molecule_local` / `ambiguous_multidrug`), and the paper detail view
says so in plain language.

**Sample size.** Prefers explicit `n=`, then `<number> patients/participants/mice`.
Distinct counts are only **summed** when there is an explicit multi-arm cue
("per group", "randomized to", "arms"); a cohort-flow cue (enrolled / screened /
analysed / completed / ITT) forces the **maximum** instead, because
"n=50 enrolled … n=48 analysed" is one cohort at two stages, not 98 people.

**Reviews and meta-analyses are parsed differently.** They have no single cohort or
dose, so they use a separate parser reporting **k included studies + pooled
participants** ("12 studies; 4,530 participants"), recognising the phrasings reviews
actually use ("Twelve trials", "23 RCTs", `n=3,201`). A single "dose" is meaningless
for a review and is not asserted.

**Route.** Normalised vocabulary (oral, subcutaneous, intravenous, intraperitoneal,
intramuscular, intranasal, topical, inhaled, infusion). Animal studies legitimately
use several routes across cohorts, so multiple routes are kept.

**Duration.** Requires a study/treatment context word and rejects age distractors
("aged 18 years" is not a study duration) using a window local to the number.

## 3. Outcome direction

`beneficial / harmful / neutral / unclear`, from efficacy and safety sentences.
Matching is **negation-aware**: "no reduction in mortality" is neutral, not harmful;
"no serious adverse events" is not a harm signal. Direction-ambiguous words are not
trusted alone — bare "mortality" says nothing, so harm requires a directional phrase
("increased mortality"), while "reduced mortality" reads as beneficial.

This field is **deliberately rules-only**. Authors systematically frame their own
findings positively, so a language model reading that framing over-calls
"beneficial"; measured agreement between a model and these rules was 1 in 81 papers,
in that direction.

## 4. Automated rigor (within-class quality, 0–100)

A class-appropriate rubric — a randomised human trial and an in-vitro assay are not
judged on the same criteria. Starts from a class base score and adds points for
design features detectable in the text: randomisation, blinding, comparator/controls,
sample size, replication, dose-response, follow-up.

Credits are **negation- and context-aware**: a study gets no blinding points for
"an open-label study, unlike double-blind trials", none for "not randomized", and
none for a term inside a larger word ("unblinded"). Where the abstract is
structured, design terms are read from its **Methods** section so background or
citation mentions can't leak credit.

This is **not** a formal risk-of-bias assessment (not Cochrane RoB 2, not ROBINS-I)
and **not** GRADE. Formal RoB is not assessed; the site says so on every record.

## 5. Directness (translational level)

How directly the evidence bears on humans: human RCT high → human observational →
animal → in vitro low. Independent of rigor: a flawless cell study is still
indirect, and a mediocre human trial is still about humans. Where NIH iCite provides
human/animal/molecular proportions, those are preferred over keyword inference.

## 6. Rank (ordering only)

A weighted blend summing to 1.00: directness 0.33, rigor 0.28, relevance 0.20,
recency 0.10, impact 0.05, venue 0.04. Every component is emitted per record in
`rank_components`, so any position in the list can be explained. Impact uses iCite
NIH percentile, then RCR (log-scaled), then raw citation count. Venue is neutral for
unknown journals so an unfamiliar journal is never penalised. Rank is a **sorting
aid, not a verdict**.

## 7. Publication, capping and dedup

Records are `featured / listed / review / excluded_noise` by completeness and
strength. The published feed is capped per molecule and section to bound page
weight, but **human evidence and evidence syntheses are never dropped**, nor is
anything at/above the 90th iCite percentile. Molecules with little literature are
exempt from the cap entirely. A paper matched to the same molecule by several search
rules is **de-duplicated** to one record, keeping the best-ranked instance.

## 8. Validation gates (what blocks a bad build)

`scripts/validate_curated.py` runs as a hard deploy gate: score ranges, tier and
status vocabularies, unique evidence ids, species vocabulary, rank range, a
schema-drift canary (rows whose JSON failed to parse), and corpus-collapse anomaly
checks against the previous known-good build (a >50% drop in papers, molecules or
published records fails the build and keeps the last good site up).

---

## Why this can stand without AI

The measured bottleneck is **input, not intelligence**. An audit of the corpus
(`scripts/audit_missing_fields.py`) found that where a field is missing, roughly
95% of the time the abstract simply never states it — no extractor of any kind
could recover it — while only ~4% are genuine rule gaps.

When a language model was given the same papers, its dose extractions were verified
correct 12/12 — but every one of those wins was reproduced deterministically once
the rules were given (a) the same open-access full text and (b) proximity-based
attribution. The model's advantage was not comprehension; it was better input and a
smarter attribution rule, both of which are expressible as rules.

That matters for a database whose value is trustworthiness: rules can be read,
tested, and argued with; they fail the same way twice; and they never invent a dose
that isn't in the source. Every guard above exists because a specific wrong value
was observed and fixed — which is a form of accuracy a model cannot offer without
re-verification on every run.
