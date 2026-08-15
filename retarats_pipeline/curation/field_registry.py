"""Single source of truth for per-record fields.

Historically the set of fields lived in several hand-maintained allowlists that had
to be kept in sync by hand:

* ``SITE_JSON_FIELDS`` in ``scripts/build_curated_database.py`` — what gets written
  into ``site_data.json`` (the feed the site downloads).
* ``RECORD_FIELDS`` in ``scripts/build_public_site.py`` — what the UI reads off each
  record (anything not listed is stripped).
* plus the modal grid and card-pill rendering, edited separately.

Keeping those in sync manually is the friction that made every new feature expensive,
and it already drifted: two iCite fields are read by the UI but never emitted to the
feed, so their modal rows are always blank. This module makes the field set declared
ONCE. To add a new field, add ONE entry here and set its flags; the build and the UI
derive their lists from it.

Migration is staged so it is provably non-destructive:
* Phase 1.1 (now): declare the registry to reproduce the CURRENT lists exactly; a
  test locks that fidelity. Nothing is wired in yet.
* Phase 1.2: `build_curated_database` / `build_public_site` import their lists from here.
* Phase 1.3/1.4: drive the modal grid and card pills from `modal` / `card` flags.

Flags per field:
* ``site_json`` — emitted into site_data.json (the feed).
* ``record``    — read by the UI record normalizer (RECORD_FIELDS allowlist).
* ``modal``     — (reserved for 1.3) show in the paper detail grid.
* ``card``      — (reserved for 1.4) show as a preview pill on the card.
* ``group`` / ``label`` — (reserved) human-facing grouping/label for rendering.
"""

from __future__ import annotations

from typing import List, NamedTuple


class Field(NamedTuple):
    key: str
    site_json: bool = True
    record: bool = True
    modal: bool = False
    card: bool = False
    group: str = ""
    label: str = ""


# Order follows the current RECORD_FIELDS so `record_fields()` is byte-identical to
# today's list. `site_json` is False for exactly the two fields that the feed does
# NOT currently emit (the known drift). label/group/modal/card are filled in later
# phases; leaving them default now changes nothing.
_F = Field
FIELDS: List[Field] = [
    _F("molecule_id"), _F("molecule_name"), _F("pmid"), _F("doi"), _F("title"),
    _F("journal"), _F("pub_year"), _F("website_section"), _F("evidence_class"),
    _F("evidence_class_label"), _F("publication_status"), _F("authors_short"),
    _F("first_author"), _F("author_count"), _F("citation_count"),
    _F("journal_reputation"), _F("journal_tier"), _F("reliability_score"),
    _F("reliability_tier"), _F("evidence_directness"), _F("directness_tier"),
    _F("reliability_components"), _F("rank_components"), _F("rank_score"),
    _F("rank_tier"), _F("appraisal_summary"), _F("appraisal_strengths"),
    _F("appraisal_limitations"), _F("refined_dose"), _F("refined_route"),
    _F("refined_duration"), _F("refined_sample_size"), _F("refined_outcome_direction"),
    _F("refined_extraction_scope"), _F("facet_species"), _F("facet_indication"),
    _F("facet_endpoint"), _F("facet_study_type"), _F("facet_model_system"),
    _F("facet_route"), _F("facet_drug_class"), _F("facet_population"), _F("facet_sex"),
    _F("facet_formulation"), _F("facet_evidence_direction"), _F("facet_evidence_impact"),
    _F("facet_clinical_article"), _F("facet_research_article"),
    _F("facet_translational_compartment"), _F("is_retracted"), _F("is_corrected"),
    _F("facet_publication_flag"), _F("facet_all"),
    _F("icite_nih_percentile"), _F("icite_apt"), _F("icite_is_clinical"),
    _F("icite_clinical_influence"), _F("icite_x_coord"), _F("icite_y_coord"),
    _F("icite_rcr"), _F("icite_human"), _F("icite_animal"), _F("icite_molecular"),
    # Previously drifted (UI-read but not fed, so their modal rows were always blank).
    # Fixed by adding them to the feed now that the registry drives it -- they are
    # merged upstream (PAPER_MERGE_FIELDS) and read by the modal ("Field citation
    # rate", "iCite citations"), so this populates those two rows.
    _F("icite_field_citation_rate"),
    _F("icite_citation_count"),
]


def record_fields() -> List[str]:
    """Fields the UI reads off each record (the RECORD_FIELDS allowlist)."""
    return [f.key for f in FIELDS if f.record]


def site_json_fields() -> List[str]:
    """Fields written into site_data.json (the feed)."""
    return [f.key for f in FIELDS if f.site_json]
