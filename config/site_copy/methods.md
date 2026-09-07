## The short version

RetaBase reads the published literature on each bioactive (the retatrutide-family molecules this database tracks) and puts the strongest, most human-relevant evidence first. It does this with a fixed, written rubric — no black-box model decides what ranks. Every **record** — a single paper — carries three numbers you can inspect: where it sits on the **evidence pyramid** (its study design), how well it was **run** for its type (rigor), and how directly it applies to **people** (directness). A combined **rank** blends those three with how on-topic the paper is (relevance), how recent it is, how often it is cited, and how well-regarded its journal is, to order the feed.

Read the numbers together. The evidence level tells you *what kind* of study it is; rigor tells you *how well* it was done; directness tells you *how relevant* it is to humans. A tidy lab study and a large clinical trial are never scored on the same curve.

> These are automated, rule-based signals for triage and best-first ordering. They are **not** a formal risk-of-bias assessment (Cochrane RoB 2, ROBINS-I) and **not** a GRADE certainty rating. No human reviewer appraises each study.

## The evidence pyramid

Not all study designs carry equal weight. RetaBase places every record on an evidence pyramid and orders the feed by that **level first**, then by the rank score **within** a level. So a case report never outranks a randomized trial in the default view, and rigor and impact only ever compare like with like.

The pyramid runs, top to bottom, from systematic review down through meta-analysis, practice guideline, randomized trial, cohort, case-control, case series, case report, narrative review, and finally animal and in-vitro work. (That is the abbreviated walk; the full 14-level ladder — which adds non-randomized trials and cross-sectional studies — is drawn in the Guide.)

The level is detected from the paper's PubMed publication type and shown as a level badge (for example "L4") on each card. You can switch the feed between *level-first* and *mixed* ordering, and filter to a single level, in the sidebar.

:::detail Show the sources behind the ladder

The ladder follows established evidence hierarchies. For human studies it aligns with the Oxford Centre for Evidence-Based Medicine [2011 Levels of Evidence](https://www.cebm.net/wp-content/uploads/2014/06/CEBM-Levels-of-Evidence-2.1.pdf), where systematic reviews and randomized trials sit at the top for treatment questions. It extends the widely-taught [evidence pyramid](https://guides.library.ucdavis.edu/systematic-reviews/levels-of-evidence) downward to the animal and in-vitro tiers that clinical hierarchies leave out but which this database contains.

This is a "where does the strongest evidence sit" shortcut based on study **design**. It is deliberately *not* a formal certainty rating like [GRADE](https://www.gradeworkinggroup.org/), which judges confidence in a specific effect after the relevant studies have been gathered and appraised. The full 14-level ladder lives in an editable config file (`config/evidence_hierarchy.csv`), so the ordering can be audited and adjusted.

:::

## Rigor — how well a study was run

Rigor (0–100) measures how well a study was conducted **for its own type of study** (its "evidence class"). A randomized human trial and an in-vitro assay are judged on different rubrics: the score starts from a baseline for that study type (its "class base") and only ever *adds* points for design features detected in the reported methods and abstract — nothing is subtracted.

```formula
rigor = class base + design credits, capped at 100
```

Because each type is graded on its own curve, the number is **not comparable across classes**: a high-rigor cell study is not stronger evidence than a lower-rigor trial. Credit for the core design features — blinding, randomization, and controls — is negation-aware and read from the **Methods** section: a study earns no blinding credit for "an open-label study, unlike double-blind trials…", and a technique named only in the introduction of a structured abstract doesn't count. (Other credits, such as sample size or dose-response, are matched across the whole abstract.)

:::detail Show every credit, by study type

Each variable and the points it adds are enumerated here. The sum is capped at 100 and stored with a per-signal breakdown, so any card's rigor traces back to the exact credits it earned. Sample-size credit is 0 when no size is reported (a reported-but-small size still earns the smallest tier).

#### Human trials & observational studies

Base **design**: controlled trial 60, non-randomized interventional 45, observational 34. **Comparator**: placebo +12, active / standard-of-care (or any reported comparator) +8. **Blinding**: double +8, single +4. **Randomization** +6 (credited for non-RCTs that report it; RCTs already carry it in the design base). **Sample size**: n≥1000 +14, ≥300 +11, ≥100 +8, ≥30 +4, smaller +1. **Extras** +4 for multicentre / prospective / pre-registered.

#### Evidence syntheses

Base **design** 60. **Systematic search** +12 (PRISMA / PROSPERO / predefined search). **Quantitative pooling** +8 (meta-analysis / random-effects / forest plot). **Evidence base**: ≥10 included studies +12, ≥3 +6, else +3. **Appraisal** +6 (GRADE / risk-of-bias / Cochrane). **Consistency** +4 (low heterogeneity).

#### Preclinical (in vivo / animal)

Base 45. Randomization +8, blinding +8, controls +8 (vehicle / sham / littermate). **Sample size**: n≥40 +8, ≥16 +6, ≥8 +4, ≥4 +2. Dose-response +10, time-course +4, replication +8, in-vivo outcome +5.

#### In vitro / mechanistic

Base 40. Controls +12. **Orthogonal methods**: ≥3 techniques +14, 2 +8. Dose-response +12, replication +12, physiological relevance +10 (primary / patient-derived cells, organoids).

#### Narrative reviews & methods / assay papers

**Narrative review**: base 45, plus +10 for a comprehensive / critical / up-to-date scope. **Methods / assay / tool**: base 40, plus validation metrics (sensitivity / specificity / limit of detection) +18, comparison to a reference or gold-standard method +14, reproducibility (inter- / intra-assay) +10, novelty / utility +8. A record that matches none of the class rubrics falls back to a fixed 30.

:::

## Directness — how relevant it is to people

Directness (0–100) measures how directly a finding applies to humans, independent of how well the study was run. It is set by evidence class, so it is predictable rather than a judgement call. The values in the table below are the base for each class. For the preclinical, in-vitro, "methods/tool", and "other" classes only, a small bounded nudge from NIH iCite's translation-potential score (APT) can move the value up to +8 or down to −4 — enough to re-order records *within* a class, never enough to leapfrog human or synthesis evidence.

:::detail Show the directness score for every study type

| Evidence class | Directness |
| --- | --- |
| Human — controlled trial (RCT) | 95 |
| Clinical practice guideline | 92 |
| Evidence synthesis (systematic review / meta-analysis) | 90 |
| Human — interventional (non-RCT) | 80 |
| Human — observational | 66 |
| Preclinical (in vivo / animal) | 45 |
| Narrative review | 42 |
| In vitro / molecular | 25 |
| Other / unclear | 22 |
| Methods / assay / tool | 16 |

:::

## Rank — the best-first order

Rank (0–100) blends the axes above with relevance, recency, citation impact and journal venue into the single score that orders the feed *within* each evidence level:

```formula
rank = 0.30·directness + 0.28·rigor + 0.18·relevance + 0.10·recency + 0.10·impact + 0.04·venue
```

Impact is time-normalized through the NIH [iCite](https://icite.od.nih.gov/) percentile, so a strong recent paper isn't buried by older, more-cited ones.

:::detail Show each axis, its weight, and why

| Axis | Weight | What it is | How it is derived |
| --- | --- | --- | --- |
| Directness | 0.30 | how directly it applies to humans | set by evidence class (RCT 95 … in-vitro 25) |
| Rigor | 0.28 | within-class study quality | the rubric above |
| Relevance | 0.18 | how central the bioactive is to the paper | the molecule's role (direct intervention scores highest) |
| Recency | 0.10 | newer evidence ranked higher | publication year, scaled from a 1990 anchor |
| Impact | 0.10 | citation impact | iCite percentile, else Relative Citation Ratio, else a raw count |
| Venue | 0.04 | journal standing | a curated journal-reputation table |

**Why these weights.** Directness and rigor are deliberately dominant — 58% together — because study design and conduct should drive ordering more than popularity. Recency is capped at 10% and never falls to zero for old work, so foundational papers aren't buried for their age. Impact prefers iCite's time- and field-normalized percentile and defaults to 0 (never negative) until a citation backfill runs, so it only ever promotes well-cited work. Venue is kept as its own small 4% axis rather than folded into impact — folding would inflate a zero impact axis to ~50 across the whole database — and unknown journals get a neutral 50, so a good study in an obscure journal is never sunk. The weights live in an editable config so they are easy to audit and tune.

:::

## How to read the scores (and what not to read in)

The scores are a **triage aid** — they order what's worth reading first. They are not a verdict on any single paper, and a few patterns are worth keeping in mind so they don't mislead you:

- **A low Impact ring often just means "new," not "weak."** Impact is citation-based, and citations take years to accumulate — so a strong, recent paper can show a low Impact simply because the field hasn't cited it yet. We soften this two ways (Impact uses iCite's time- and field-normalized percentile, and it never drops a paper's rank to zero), but the effect can't be erased. A brand-new study with no citations shows Impact 0, which means *not yet measured*, not *no impact*.
- **Older papers can look stronger on citations for the same reason** — they've had more time to be cited. That doesn't make newer work worse; it has just been around less long. Recency is a deliberate counterweight, but read citation-driven signals with the publication year in mind.
- **Rigor is only comparable within a class.** A high-rigor in-vitro study is *not* stronger evidence than a lower-rigor human trial — they're scored on different curves. Compare rigor only between papers of the same type.
- **Evidence level reflects study design, not this study's execution.** A flawed RCT still sits above a strong cohort by design. Use the level for the *kind* of evidence, then rigor and the paper itself to judge quality *within* that level.
- **Absence of evidence is not evidence of absence — or of safety.** Few records for a bioactive, or no reported harms, usually means it is understudied, not that it is safe or ineffective.
- **Venue barely moves the score, and obscurity isn't penalized.** Journal reputation is a small 4% factor, and unknown journals get a neutral value — a good study in a little-known journal is never sunk.

The bottom line: the ordering points you at the strongest, most human-relevant evidence first, but it is not a substitute for reading the study. Every number on a card can be traced to its inputs, so you can always check *why* something ranks where it does.

## Evidence classes & guidelines

Each record is classified from its PubMed publication type, study-design signals, and the human / animal / molecular classification (NLM's "Triangle of Biomedicine") that iCite provides. The class sets both the rigor rubric and the directness value.

Clinical practice guidelines are a special case. They are authoritative synthesized recommendations, not primary studies, and grading their quality needs a separate formal instrument (AGREE II) whose inputs we don't have. So RetaBase shows rigor as **n/a** for guidelines rather than a misleading number, treats them as high-directness, and ranks them near the top.

## What gets featured

Every record is included, but sorted into visibility tiers:

- **Featured** — directly relevant to people with at least moderate rigor, or a strong synthesis / guideline.
- **Listed** — included, lower priority.
- **Review** — missing required metadata, flagged for a curator.
- **Excluded as noise** — off-topic / non-biomedical; the only hard exclusion.

On the Bioactive overview, a molecule's count of Featured records is surfaced as its **"spotlight papers"** — its strongest, most human-relevant evidence.

## Where the data comes from

Every field is traceable to a public source, and each is refreshed by a scheduled job.

- **Papers, abstracts & publication types** — PubMed via the NCBI E-utilities. The publication type is what determines the evidence level.
- **Citation impact** — the NIH [iCite](https://icite.od.nih.gov/) API (percentile, Relative Citation Ratio, and the clinical-article flag), with [OpenAlex](https://openalex.org/) as a fallback raw citation count.
- **Trials** — [ClinicalTrials.gov](https://clinicaltrials.gov/).
- **Preprints** — bioRxiv / medRxiv via [Europe PMC](https://europepmc.org/).
- **Regulatory status** — a curated table where every row carries its source and retrieval date, plus direct per-drug links into [DailyMed](https://dailymed.nlm.nih.gov/) (the FDA label) and [Drugs@FDA](https://www.accessdata.fda.gov/scripts/cder/daf/) (approvals).
- **Chemical identity** — [PubChem](https://pubchem.ncbi.nlm.nih.gov/).
- **Journal reputation (venue)** — a small curated allowlist of high-reputation biomedical journals, **not** a purchased impact factor; unknown journals get a neutral score and are never penalised.

## Regulatory information & safety

RetaBase documents what the published literature and public registries report — including what people are reported to be doing — so readers can see the evidence and the regulatory picture in one place. It is **not medical advice**, creates no clinician–patient relationship, and is not a guide to obtaining anything.

We are explicitly against the use of these substances without a qualified clinician. Many interact with prescription medicines, several have contraindications that depend on individual history, and several are studied precisely because their risks are not yet characterised. An absence of reported harms in this database is not evidence of safety — it frequently means nobody has looked.

Regulatory status varies by country and changes over time; every status shown carries its source and retrieval date and may already be out of date. Each bioactive links directly into [DailyMed](https://dailymed.nlm.nih.gov/) and [Drugs@FDA](https://www.accessdata.fda.gov/scripts/cder/daf/); neither is a complete index, so both are provided. Legality differs by jurisdiction and is the reader's responsibility. Nothing here is encouragement to obtain a substance through compounding, research-chemical, or grey-market channels.

## Reproducibility & how to cite

Every metric is rule-based, and the underlying feed and scoring code can be inspected and reproduced. The corpus fingerprint is a deterministic hash of the exact corpus composition, so a citation can name the version it saw; the full SQLite corpus is snapshotted weekly (compressed + SHA-256) as a release asset.
