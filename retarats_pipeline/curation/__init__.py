"""Curation layer: faceting, evidence strength, publication status, and appraisal.

These modules turn the broad internal evidence database into a filterable,
publishable, backend-agnostic curated layer (Google Sheets now, Airtable later).
All logic is rule-based and auditable; nothing here calls an LLM or the network.
"""

from .facets import derive_facets, load_facet_defs, FACET_GROUPS
from .strength import score_reliability
from .publication_status import decide_publication
from .appraisal import appraise_evidence

__all__ = [
    "derive_facets",
    "load_facet_defs",
    "FACET_GROUPS",
    "score_reliability",
    "decide_publication",
    "appraise_evidence",
]
