## The short version

RetaBase reads the published literature on each bioactive (the retatrutide-family molecules this database tracks) and sorts it by **study design**. It does this with fixed, written rules — no black-box model decides what ranks. Every **record** — a paper linked to a bioactive — is placed on the **evidence pyramid** (its study design, for example systematic review, randomized trial, cohort or animal study). The default order is evidence level first, then an automated **ordering score** within a level.

What the site does **not** do: it does not appraise how well any individual study was conducted, and it does not give a validated quality, strength or confidence score. The ordering score, and the other 0–100 values kept in the data files, are legacy automated heuristics. They are not validated, they are not comparable across different kinds of study, and they are not shown on the cards.

> These are automated, rule-based signals for sorting and triage. They are **not** a formal risk-of-bias assessment (Cochrane RoB 2, ROBINS-I) and **not** a GRADE certainty rating. No human reviewer appraises each study. Study type is assigned automatically and can be wrong.

## The evidence pyramid

Not all study designs carry equal weight. RetaBase places every record on an evidence pyramid and orders the feed by that **level first**, then by the automated ordering score **within** a level. For records that are classified correctly, a case report is therefore listed below a randomized trial in the default view. Because the level is assigned automatically, a record can sit at the wrong level — for example a review or cohort study labelled a trial, or a letter labelled a study.

The clinical ordering runs from human evidence syntheses through practice guidelines, randomized trials, and observational designs. Nonclinical syntheses remain with nonclinical evidence; mixed investigations, mixed syntheses, and unresolved review scopes have separate groups. The Guide shows the full ordering. Systematic review and meta-analysis are compatible method tags even when one primary display level is selected.

The level is taken from the paper's PubMed publication type where one is present, and shown as a level badge (for example "L4") on each card. Where the publication type is silent, it comes from keyword rules on the title and abstract and from NIH iCite's human / animal / molecular classification, which is lower confidence. You can filter to a single level in the sidebar.

:::detail Show the sources behind the ladder

The ladder follows established evidence hierarchies. For human studies it aligns with the Oxford Centre for Evidence-Based Medicine [2011 Levels of Evidence](https://www.cebm.net/wp-content/uploads/2014/06/CEBM-Levels-of-Evidence-2.1.pdf), where systematic reviews and randomized trials sit at the top for treatment questions. It extends the widely-taught [evidence pyramid](https://guides.library.ucdavis.edu/systematic-reviews/levels-of-evidence) downward to the animal and in-vitro tiers that clinical hierarchies leave out but which this database contains.

This is a "which kind of study is this" shortcut based on study **design**. It is deliberately *not* a formal certainty rating like [GRADE](https://www.gradeworkinggroup.org/), which judges confidence in a specific effect after the relevant studies have been gathered and appraised. The display ordering lives in an editable config file (`config/evidence_hierarchy.csv`), so the ordering can be audited and adjusted.

:::

## How records are ordered

The default feed order is: evidence level first (the pyramid above), then the **ordering score** within each level, then the original feed order for ties. The ordering score is not a quality score. It puts on-topic, recent, well-cited records of the same level first, and it also contains legacy values that depend on the kind of study.

```formula
ordering score = 0.30·study-type value + 0.28·reported-features value + 0.18·relevance + 0.10·recency + 0.10·impact + 0.04·venue
```

:::detail Show each input, its weight, and what it really is

| Input | Weight | What it actually is |
| --- | --- | --- |
| Study-type value (stored as "directness") | 0.30 | A fixed number for the record's study type (for example 95 for a controlled human trial, 25 for an in-vitro study). It ignores the molecule's role in the paper and is not a measure of how directly the evidence applies. |
| Reported-features value (stored as "rigor") | 0.28 | A fixed starting number for the study type plus credits for design keywords found in the title or abstract (for example "double-blind"). About half of records get no keyword credit at all, and a keyword can fire on an unrelated word. Missing information scores 0; that is not a finding that the feature was absent. |
| Relevance | 0.18 | How central the bioactive appears to be to the paper, from the molecule's role in it. |
| Recency | 0.10 | Newer publication years score higher, scaled from a 1990 anchor. |
| Impact | 0.10 | Citation-based: the NIH iCite percentile, else its Relative Citation Ratio, else a raw citation count. iCite percentiles are mostly not available yet for papers from the last two years, so those papers receive a lower impact value than their citations would suggest. They are listed lower within a level than older papers. |
| Venue | 0.04 | A name match against a curated journal list. The matching can mislabel some journals. Unknown journals get a neutral value. |

The weights are fixed in the code (`retarats_pipeline/curation/ranking.py`), not in an editable config. Only the pyramid ordering is a config file.

:::

Because the study-type value and the starting numbers are constants for each kind of study, **none of these 0–100 values can be compared across different kinds of study**. A 70 for a cell study does not mean it is as good as a 70 for a trial. RetaBase therefore does not sort or filter by them across study types, and does not display them on cards. They stay in the data files for transparency and for future validation work.

Records without an abstract may be assigned a study type with less confidence, and the legacy values come out lower for them. That reflects missing information, not poor study quality.

:::detail Show what the reported-features value looks for

The sum is capped at 100 and stored with a per-signal breakdown. Credits for blinding, randomization and controls are negation-aware ("an open-label study, unlike double-blind trials…" earns no blinding credit) and are read from the Methods part of a **structured abstract** when there is one. For unstructured abstracts, the whole title and abstract are searched. The paper's own Methods section is **not** read for these credits. Open-access full text is used only to extract sample size, dose, route and duration. A sample-size credit is 0 when no size could be read; a missing size is not a small size.

#### Human trials & observational studies

Starting value: controlled trial 60, non-randomized interventional 45, observational 34. **Comparator**: placebo +12, active / standard-of-care (or any reported comparator) +8. **Blinding**: double +8, single +4. **Randomization** +6 (for non-RCTs that report it). **Sample size**: n≥1000 +14, ≥300 +11, ≥100 +8, ≥30 +4, smaller +1. **Extras** +4 for multicentre / prospective / pre-registered.

#### Evidence syntheses

Starting value 60. **Systematic search** +12 (matches words such as "systematic" and PRISMA / PROSPERO). **Quantitative pooling** +8. **Evidence base**: ≥10 included studies +12, ≥3 +6, else +3. **Appraisal** +6 (GRADE / risk-of-bias / Cochrane). **Consistency** +4 is matched on the word "consistent", not on a heterogeneity statistic.

#### Preclinical (in vivo / animal)

Starting value 45. Randomization +8, blinding +8, controls +8 (vehicle / sham / littermate). **Sample size**: n≥40 +8, ≥16 +6, ≥8 +4, ≥4 +2. Dose-response +10, time-course +4, in-vivo outcome +5, and a **replication** keyword credit of +8 that can fire on unrelated words.

#### In vitro / mechanistic

Starting value 40. Controls +12. **Orthogonal methods**: ≥3 techniques +14, 2 +8. Dose-response +12, a **replication** keyword credit of +12 that can fire on unrelated words, and physiological relevance +10 (primary / patient-derived cells, organoids).

#### Narrative reviews & methods / assay papers

**Narrative review**: starting value 45, plus +10 for a comprehensive / critical / up-to-date scope. **Methods / assay / tool**: starting value 40, plus validation metrics +18, comparison to a reference method +14, reproducibility +10, novelty / utility +8; these keyword matches are generic. A record that matches none of the class rubrics falls back to a fixed 30.

:::

## How to read this site (and what not to read in)

The ordering is a **triage aid**. It is not a verdict on any single paper, and a few patterns are worth keeping in mind so they don't mislead you:

- **Evidence level reflects study design, not this study's execution.** A flawed RCT still sits above a strong cohort by design. Use the level for the *kind* of study, then read the paper to judge how well it was done.
- **The study type is assigned automatically and can be wrong.** Known patterns: reviews or cohort studies labelled as trials, letters, comments and errata labelled as studies, and cell or animal papers labelled human when the text mentions patients. Use the ⚑ Report link on a card when you spot one.
- **Missing information is not poor quality.** If no abstract or full text is available, or a sample size or dose could not be read, the record says it was *not found in the available text*. That does not mean the study did not report it or did not do it.
- **The level does not say what the bioactive's role was.** A record can be listed because the bioactive was a comparator, background therapy, an assay reagent or a measured outcome, not the thing being tested. Check the molecule's role in the paper.
- **One trial can appear many times.** A large trial's primary report and its secondary analyses are separate records, so the count of records is not a count of independent studies.
- **A low citation count often just means "new," not "weak."** Citations take years to accumulate, and the iCite percentile is mostly not yet available for recent papers. Older papers look better on citations for the same reason. That does not make newer work worse.
- **Absence of evidence is not evidence of absence — or of safety.** Few records for a bioactive, or no reported harms, usually means it is understudied, not that it is safe or ineffective.
- **Venue is not a quality signal here.** Journal name is shown as bibliographic information only. A small 4% input to the legacy ordering score comes from a name list that can mislabel some journals, and no venue tier is displayed.

The bottom line: the ordering points you at higher study-design tiers first, but it is not a substitute for reading the study. Each automated value can be traced to its rule inputs, so you can check *why* something is listed where it is.

## Evidence classes & guidelines

Each record is classified from its PubMed publication type, study-design signals in the title and abstract, and the human / animal / molecular classification (NLM's "Triangle of Biomedicine") that iCite provides. The class sets the study-type value and which keyword credits apply. Classification is automated and can be wrong; where the publication type is missing it relies on keyword rules, which are lower confidence.

Clinical practice guidelines are a special case. They are synthesized recommendations, not primary studies, and judging their quality needs a formal instrument (AGREE II) whose inputs we don't have. So RetaBase shows no reported-features value for guidelines (n/a). Guidelines are recognised from the PubMed publication type or, when that is missing, from title patterns, so a few expert consensus statements may be included.

## What gets featured

Every record is included, but sorted into visibility labels:

- **Featured** — an automatic label, not an endorsement. A record is Featured when the automated classification places it in a human controlled-trial class (or another human interventional class), as an evidence synthesis, or as a practice guideline, and its legacy values pass fixed cutoffs. Featured does **not** mean the study is of high quality, and it does not mean the bioactive is the intervention being tested. Retracted records, PubMed-indexed preprints, and non-research items (letters, comments, editorials, errata) are never Featured. They stay searchable, and retracted records carry a RETRACTED badge.
- **Listed** — included, not Featured.
- **Review** — a record missing required metadata is flagged for a curator and is not shown on the public site.
- **Excluded as noise** — off-topic / non-biomedical; the only hard exclusion.

## Where the data comes from

Every field is traceable to a public source, and each is refreshed by a scheduled job.

- **Papers, abstracts & publication types** — PubMed via the NCBI E-utilities. The publication type is the main input to the evidence level, where PubMed has assigned one.
- **Citation impact** — the NIH [iCite](https://icite.od.nih.gov/) API (percentile, Relative Citation Ratio, and the clinical-article flag), with [OpenAlex](https://openalex.org/) as a fallback raw citation count.
- **Trials** — [ClinicalTrials.gov](https://clinicaltrials.gov/).
- **Preprints** — bioRxiv / medRxiv via [Europe PMC](https://europepmc.org/).
- **Regulatory status** — a curated table where every row carries its source and retrieval date, plus direct per-drug links into [DailyMed](https://dailymed.nlm.nih.gov/) (the FDA label) and [Drugs@FDA](https://www.accessdata.fda.gov/scripts/cder/daf/) (approvals).
- **Chemical identity** — [PubChem](https://pubchem.ncbi.nlm.nih.gov/).
- **Journal reputation (venue)** — a small curated list of journal-name patterns, used only as a minor input to the legacy ordering score. It is **not** a purchased impact factor, the name matching can mislabel some journals, and no venue tier is shown on the site.

## Regulatory information & safety

RetaBase documents what the published literature and public registries report — including what people are reported to be doing — so readers can see the evidence and the regulatory picture in one place. It is **not medical advice**, creates no clinician–patient relationship, and is not a guide to obtaining anything.

We are explicitly against the use of these substances without a qualified clinician. Many interact with prescription medicines, several have contraindications that depend on individual history, and several are studied precisely because their risks are not yet characterised. An absence of reported harms in this database is not evidence of safety — it frequently means nobody has looked.

Regulatory status varies by country and changes over time; every status shown carries its source and retrieval date and may already be out of date. Each bioactive links directly into [DailyMed](https://dailymed.nlm.nih.gov/) and [Drugs@FDA](https://www.accessdata.fda.gov/scripts/cder/daf/); neither is a complete index, so both are provided. Legality differs by jurisdiction and is the reader's responsibility. Nothing here is encouragement to obtain a substance through compounding, research-chemical, or grey-market channels.

## Reproducibility & how to cite

Every automated value is rule-based, and the underlying feed and classification code can be inspected and reproduced. The corpus fingerprint is a deterministic hash of the exact corpus composition, so a citation can name the version it saw; the full SQLite corpus is snapshotted weekly (compressed + SHA-256) as a release asset.


## Categories and source evidence

Research areas, studied conditions, measured outcomes, experimental systems, and synthesis methods are independent filters. Condition/use topics and outcome topics retain the older broad keyword tags. A topic mention does not establish that a condition was studied or an outcome measured.

The text-supported condition and outcome fields use a limited, versioned vocabulary and conservative rules. Background mentions, exclusions, and possible future uses do not by themselves establish a studied condition. A blank field means no supported annotation was extracted; it does not mean the study omitted that topic or measurement. Numerical effects and benefit/harm conclusions are not inferred by this layer.

Open a paper's Category evidence section to inspect the source passage for each new annotation. These annotations are automated and have not been verified by a human curator. The vocabulary supplies definitions, assignment criteria, and counterexamples. Mixed and unresolved scopes are retained in the overall evidence map, with review flags. The Human data view admits syntheses only when their included evidence has explicit human scope.

The ontology development pilot contains 48 reports screened by Codex against their local titles and abstracts. It is a development sample, not independent validation, a formal risk-of-bias assessment, or a measurement of corpus-wide accuracy.
