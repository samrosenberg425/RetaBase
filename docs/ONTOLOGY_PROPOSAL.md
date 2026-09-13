# RetaBase ontology proposal

Status: design draft, 2026-09-12. This document proposes a migration; it does not change live classifications, ranking, or publication policy. Working assumption: RetaBase remains a research evidence map spanning human and preclinical studies, with accessible browsing for clinicians and other readers.

## 1. The organizing principle

Organize around an evidence question: **Which bioactive, in which population or model, for which condition or purpose, compared with what, measured by which outcome?**

Keep four layers distinct:

1. **Ontology:** the entities and relationships that represent the evidence.
2. **Vocabulary:** the permitted terms, definitions, aliases, and broader/narrower relationships.
3. **Curation criteria:** the evidence required to assign a term or make a decision.
4. **Presentation:** filters, collections, ordering, and summaries derived from those records.

A compound can belong to several browsing collections. A collection such as “Aging and longevity” describes a research interest; membership does not establish an effect on human longevity.

## 2. Core entities and relationships

| Entity | What one record represents | Important relationships |
|---|---|---|
| Bioactive | A defined substance, mixture, or explicitly defined combination | Has identifiers and aliases; related to targets; may have components |
| Study | An investigation, trial, or separable experiment | Has populations/models, arms, comparisons, outcomes; can have several reports |
| Report | A paper, preprint, registry entry, protocol, or other source | Reports on one or more studies; has versions and publication-integrity status |
| Bioactive–study role | What a bioactive does in a particular study or arm | Intervention, active comparator, background therapy, measured biomarker, tool compound, etc. |
| Finding | A reported result for an outcome, population, comparison, and timepoint | Linked to study and supporting report; effect estimate, uncertainty, analysis set, and result provenance |
| Annotation | A source-supported classification or assertion | Links an entity or relationship to a term and the evidence for that assignment |
| Evidence synthesis | A review with its own question and methods | Links to included studies when verified; preserves population/model scope and pooling methods |

The present paper × molecule evidence record remains useful as an index and export view. It should not become the unit of every future scientific assertion. Multiple papers can report the same trial, and a single paper can contain animal and cell experiments. Preserve distinct counts of reports, studies, and bioactive–report records; unresolved study links remain unresolved.

A preprint and its journal version should be linked. Do not count their results twice. Likewise, a review and the trials it includes are overlapping evidence, not independent replications.

## 3. Independent classification axes

| Axis | Starter vocabulary / structure | Assignment boundary |
|---|---|---|
| Substance form | Small molecule; peptide; protein; antibody; mixture; combination | Identity-level description; combination ingredients remain separately identifiable |
| Target and action | Target identifier + agonist / antagonist / inhibitor / modulator / other | Source-backed relationship with evidence status; separate proposed from demonstrated mechanisms |
| Biological process | Incretin signaling; autophagy; mitochondrial function; redox; senescence; etc. | A process discussed is distinguishable from a process experimentally tested |
| Research purpose | Therapeutic effect; safety; mechanism; pharmacokinetics; diagnostic/procedural use; synthesis/formulation; measurement methods | Multiple purposes allowed when actually investigated |
| Condition | Specific disease/condition terms with broader parents | Studied condition, comorbidity, exclusion criterion, and background mention are separate roles |
| Outcome | Outcome domain → construct → measurement | Keep clinical events, symptoms/function, biomarkers, PK, and experimental readouts distinguishable |
| Population | Age/life stage; sex; health state; comorbidities | Describe enrolled subjects or the analyzed subgroup; do not infer from introductory text |
| Species | Human; mouse; rat; nonhuman primate species; other taxa | Taxonomic identity; “cell line” is not a species |
| Experimental system | Living human; animal in vivo; ex vivo tissue; primary cells; cell line; organoid; cell-free; in silico | Human-derived cells do not count as a human clinical study |
| Design | Interventional / observational / synthesis / methodological, with design-specific attributes | Randomization, control, masking, follow-up direction, and pooling are separately represented |
| Bioactive role | Intervention; active comparator; background therapy; combination component; biomarker; pathway component; tool/control; reagent; mention only | Context-specific, potentially several roles across different experiments |
| Administration | Preparation/formulation; route; dose; frequency; duration | Belongs to an arm or exposure; distinguish study duration from treatment duration |
| Source state | Registry record; protocol; preprint; journal report; correction; retraction; expression of concern | Separate from scientific design, peer-review verification, and curator review state |

“Systematic review” and “meta-analysis” should be compatible attributes: systematic searching and statistical pooling answer different questions. Also record the designs and species included in the synthesis. Guidelines belong to a recommendation-document category with issuing body, date, scope, and appraisal status.

Regulatory records should link the relevant substance/product, indication, jurisdiction, source, and verification date. Development phase belongs to a development program or trial. “Off-label” describes a use relative to an approval context. “Supplement” is not a development phase. The existing regulatory source/date fields provide a useful starting point; actual legal statuses need source-specific verification.

## 4. Browsing categories

Use broad, overlapping **research areas** as entry points, then narrower condition and outcome filters:

- Metabolic and endocrine
- Cardiovascular
- Liver and gastrointestinal
- Kidney
- Neurological and cognitive
- Mental health and sleep
- Musculoskeletal and physical function
- Skin, wounds, and tissue repair
- Immune and inflammatory
- Infectious disease
- Reproductive and sexual health
- Cancer
- Aging and longevity

These are proposed navigation groups, not exclusive scientific classes. Mechanisms, safety, pharmacokinetics, and methods should be cross-cutting views. Broad groups can contain many condition terms, and a term may have more than one appropriate parent.

For example: **Metabolic and endocrine → type 2 diabetes**, with **glycemic regulation → HbA1c → change in HbA1c at a specified timepoint** as an independent outcome path. Weight measurement alone does not establish that obesity was the studied condition.

## 5. Criteria for inclusion and annotation

### Corpus inclusion

Include a report when it has a traceable identity and a meaningful biomedical relationship to a tracked bioactive. Keep intervention, safety, mechanism, PK, clinical-tool, formulation, and measurement-method evidence available in appropriate views. Exclude non-biomedical uses and incidental mentions from the main evidence map with explicit reason codes. Ambiguous substance identity goes to review. A registry entry can be included without reported results, but cannot be presented as an efficacy finding.

Eligibility does not depend on positive results, citation counts, journal prestige, or a rigor threshold. Missing abstracts or unclassified designs should have visible metadata states; they need not erase otherwise identifiable, relevant evidence.

### Assigning categories

1. A broad text or MeSH hit supports a **topic mention**. It does not by itself establish a studied condition, enrolled population, administered treatment, or measured endpoint.
2. Assign a **studied** relationship only from source text or structured metadata that supports that relationship in the current investigation.
3. Attach dose, route, and comparator to the relevant intervention/arm. When attribution is ambiguous, preserve the source passage and an unresolved state.
4. Preserve conflicting extractions for review; do not silently choose whichever source ran last.
5. Store negation and context. “Participants with diabetes were excluded” must not produce a studied-diabetes tag.

Every term needs an immutable ID, preferred label, definition, inclusion rule, exclusion rule, positive example, counterexample, external mappings where appropriate, and vocabulary version. Every annotation needs its subject, term, relationship, supporting source/locator or passage, extraction method/rule version, review state, and timestamp.

Keep **extraction status** (`not_attempted`, `extracted`, `ambiguous`, `conflicting`) separate from **source reporting status** (`reported`, `not_found_in_available_source`, `not_applicable`). Absence from an abstract is not proof that the full paper omitted a method.

### Findings

Represent outcome-specific numerical change (`increase`, `decrease`, `no_clear_difference`, `unclear`) separately from its interpretation (`favorable`, `unfavorable`, `context_dependent`, `not_assessed`). Store the comparator, timepoint, effect metric, units, interval, and analysis population when reported. A non-significant result is not automatically equivalence or proof of no effect.

Safety findings should preserve event type, severity/seriousness when reported, arm counts and denominators, exposure window, and comparative estimate. “No events observed” does not establish safety. A paper-level “mixed” label may summarize findings but must link back to them.

## 6. Evidence evaluation and ordering

Preserve the existing separation between rigor and directness, and preserve evidence-level-first browsing as an available/default clinical-effect view. Refine what those labels mean:

- **Study design:** a descriptive classification, not a quality verdict.
- **Reported methods:** source-supported randomization, masking, controls, attrition, replication, etc.; identify which were assessed and which were unavailable. Existing automated rigor can remain explicitly a heuristic, not a validated probability or formal appraisal.
- **Risk of bias:** a separate, design-appropriate assessment with reviewer and instrument provenance. Do not create it by renaming the automated score.
- **Human relevance:** species/system is useful globally; applicability to a particular question additionally depends on population, intervention, comparator, and outcome.
- **Evidence certainty:** a synthesis-level assessment for a defined question and outcome, not a universal molecule score.
- **Relevance, recency, and citation influence:** browsing/sorting signals whose missingness should remain distinguishable from zero.

For clinical-effect browsing, first require relevant human evidence or a synthesis of relevant human evidence, then apply the chosen design ordering and within-group sort. For mechanism and safety views, expose purpose-appropriate defaults. Keep guidelines easy to find in their own document group; document type alone should not assert that a guideline was appraised as authoritative or high quality.

The current ladder is a house ordering. Its systematic-review and meta-analysis levels should not promote animal-only syntheses into the highest human-relevance group. Retain overlapping synthesis attributes rather than choosing one exclusively for storage; the UI can still give each record a primary display group.

Cochrane describes certainty for a body of evidence per outcome, considering risk of bias, inconsistency, indirectness, imprecision, and publication bias. This supports the separation above; it does not validate RetaBase's point scores. [Cochrane Handbook, Chapter 14](https://www.cochrane.org/authors/handbooks-and-manuals/handbook/current/chapter-14).

## 7. Concrete issues in the current implementation

| Current location / representation | Proposed change |
|---|---|
| `config/MOLECULES.csv`: `mechanism_class` includes target classes, biological origins, proposed uses, and formulation-related terms | Split identity, target/action relationships, biological processes, and navigation collections; review ambiguous aliases rather than automatically equating related products |
| `config/MOLECULES.csv`: `status` mixes approved, clinical, off_label, research_only, and supplement | Keep development, regulatory context, and product category independently |
| `config/FACETS.csv`: `cell_line` under species | Move to experimental system; preserve the organism of origin separately |
| `config/FACETS.csv`: `obesity_weight`, `diabetes_glycemic` | Separate condition from outcome; retain broad labels as navigation mappings |
| `config/FACETS.csv`: `liver_histology` patterns include ALT/AST | Split tissue histology from blood biochemical measurements |
| `curation/facets.py`: pattern matching on title, abstract, and index terms | Retain mention tags; add relationship-specific studied/measured annotations with evidence |
| `curation/facets.py`: review is a model-system value | Move report/synthesis type out of biological system; retain scope of included systems |
| `config/ROLE_RULES.csv`: comparator/background role combines distinct uses | Distinguish active comparator, background therapy, combination component, and passing mention |
| `config/evidence_hierarchy.csv` and `curation/reliability.py` | Separate synthesis method from population scope; preserve compatible attributes and context-aware ordering |
| Paper-level outcome direction and text-derived evidence-direction facets | Consolidate around findings; derive conservative paper summaries only when supported |

These observations come from configuration and code inspection, not a measured corpus-wide error rate. Existing documentation also describes different generations of the ranking policy; reconcile methods documentation when the final policy is implemented.

## 8. Incremental implementation

**First: agree on definitions.** Add a term dictionary and annotation rules alongside the existing configs. Map existing values to retained, split, deprecated, or review-required terms. Preserve old IDs and exports for compatibility. Map conditions to MeSH where appropriate; NLM distinguishes descriptors from supplementary concepts used for drugs and other concepts. Record mapping type and version; a broad keyword association is not an exact equivalence. [NLM MeSH overview](https://www.nlm.nih.gov/oet/ed/mesh/2024/04-24_cataloging_mesh.html).

**Second: validate on a small, deliberately varied sample.** Suggested pilot: 40–60 reports covering direct interventions, comparator drugs, biomarkers, human-derived cells, mixed experiments, animal reviews, human reviews, safety reports, guidelines, and ambiguous terms. Choose compounds with different evidence profiles and substantial role ambiguity. Treat this as a feasibility sample, not proof of corpus-wide accuracy.

Review precision and missed labels separately for each critical relationship. Track disagreements and unresolved cases. Use a held-out sample before reporting accuracy. Explicit regression examples: human cells do not count as clinical evidence; a background mention does not establish an indication; a comparator dose is not assigned to the other drug; an animal review does not become human evidence.

**Third: migrate the faceting layer.** Introduce relationship/context fields and versioned mappings. Produce old and proposed classifications side by side for review, then regenerate UI filters from the same dictionary. No immediate need for a graph database or OWL; the existing SQLite architecture and linked tables can represent these relationships.

**Fourth: add structured findings.** Start with a bounded set of well-reported human studies and outcomes. Model arms, comparisons, timepoints, and report–study links before expanding extraction across the full corpus. Show study-level coverage and extraction gaps explicitly.

The first implementation milestone should be a usable dictionary, a reviewed crosswalk from current categories, and an annotated pilot with documented disagreements. That gives the ontology a concrete basis before broad reclassification.
