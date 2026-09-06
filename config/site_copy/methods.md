## How RetaBase scores the literature

Every paper is scored by a fixed, auditable rubric — no black-box model decides what ranks first. Each record carries two independent axes plus a combined rank, and a citation-impact percentile from NIH iCite. This page defines each one exactly.

> These are automated, rule-based signals meant for triage and best-first ordering. They are **not** a formal risk-of-bias assessment (such as Cochrane RoB 2 or ROBINS-I) and **not** a GRADE certainty rating. No human reviewer appraises each study.

#### Rigor — within-class study quality (0–100)

Rigor measures how well a study was conducted **for its own evidence class**. A randomized human trial and an in-vitro assay are judged on different rubrics: the score starts from a class base and adds or subtracts points for design features detected in the reported methods and abstract.

```formula
rigor = class_base + design_credits(randomization, blinding, controls, sample size, follow-up, reporting) - penalties, clamped to 0–100
```

Because each type is graded on its own curve, the number is **not comparable across classes**: a high-rigor cell study is not stronger evidence than a lower-rigor trial. Design credits are negation-aware — a study earns no blinding credit for "an open-label study, unlike double-blind trials…", and a term inside a larger word ("unblinded") is not counted.

#### Directness — human relevance (0–100)

Directness measures how directly a finding applies to humans, independent of how well the study was run. It is assigned by evidence class:

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

Read rigor and directness **together**: rigor tells you how well the study was done; directness tells you how relevant it is to people.

#### Rank — the best-first ordering

Rank blends the two axes above with topical relevance, recency, citation impact and journal venue into a single 0–100 score used to order the feed:

```formula
rank = 0.30·directness + 0.28·rigor + 0.18·relevance + 0.10·recency + 0.10·impact + 0.04·venue
```

Impact is time-normalized (via the NIH [iCite](https://icite.od.nih.gov/) percentile) so recent papers are not buried by older, more-cited ones.

## Evidence classes

Each record is classified from its PubMed publication type, study-design signals, and the NLM/MeSH translational triangle (human / animal / molecular) that iCite provides. The class sets the rigor rubric and the directness value above.

## Practice guidelines

Clinical practice guidelines are detected by their PubMed publication type ("Guideline" / "Practice Guideline"). They are authoritative synthesized recommendations, not primary studies — grading their quality is a separate formal instrument (AGREE II) whose inputs we do not have. So RetaBase shows rigor as **n/a** for guidelines rather than a misleading number, treats them as high-directness, and ranks them near the top.

## Publication status

Every record is included, but sorted into visibility tiers: **featured** (translationally direct with at least moderate rigor, or a strong synthesis / guideline), **listed** (included, lower priority), **review** (missing required metadata, flagged for a curator), and **excluded as noise** (off-topic / non-biomedical). Off-topic is the only hard exclusion. On the Bioactive overview, a molecule's count of featured records is shown as its **"spotlight papers"** — its strongest, most human-relevant evidence.

## Regulatory information & safety

RetaBase documents what the published literature and public registries report — including what people are reported to be doing — so readers can see the evidence and the regulatory picture in one place. It is **not medical advice**, creates no clinician–patient relationship, and is not a guide to obtaining anything.

We are explicitly against the use of these substances without a qualified clinician. Many interact with prescription medicines, several have contraindications that depend on individual history, and several are studied precisely because their risks are not yet characterised. An absence of reported harms in this database is not evidence of safety — it frequently means nobody has looked.

Regulatory status varies by country and changes over time; every status shown carries its source and retrieval date and may already be out of date. Each bioactive links directly into the FDA label database ([DailyMed](https://dailymed.nlm.nih.gov/)) and the approval database ([Drugs@FDA](https://www.accessdata.fda.gov/scripts/cder/daf/)); neither is a complete index, so both are provided. Legality differs by jurisdiction and is the reader's responsibility. Nothing here is encouragement to obtain a substance through compounding, research-chemical, or grey-market channels.

## Reproducibility & citation

Every metric is rule-based and the underlying feed and scoring code can be inspected and reproduced. The corpus fingerprint is a deterministic hash of the exact corpus composition, so a citation can name the version it saw; the full SQLite corpus is snapshotted weekly (compressed + SHA-256) as a release asset. Trials come from [ClinicalTrials.gov](https://clinicaltrials.gov/) and preprints from bioRxiv/medRxiv via [Europe PMC](https://europepmc.org/).
