"""Combined ranking so results show the most reliable + impactful evidence first.

Users want a single ordering that surfaces the strongest, most relevant papers
at the top. No single axis captures that, so ``rank_score`` (0-100) is a
transparent weighted blend of six axes, each already computed upstream:

    directness   33%   translational evidence level (human RCT > ... > in vitro)
    quality      28%   within-class study quality (reliability_score)
    relevance    20%   how central the molecule is to the paper (role/relevance)
    recency      10%   newer evidence ranked higher
    impact        5%   citation/attention when available (0 until backfilled)
    venue         4%   journal reputation (curated, auditable; neutral ~50 default)

**Design decision (venue vs impact):** journal reputation is kept as its own
small 4% ``venue`` axis rather than folded into ``impact``. Folding was
considered but rejected: ``impact`` is citation-driven and is 0 for every record
until the citation backfill runs, whereas ``venue`` is available (and ~50 for
unknown journals) for *every* record. Blending them would inflate the impact axis
from 0 to ~50 across the whole DB and quietly change every rank_score. A separate,
lightly-weighted axis keeps both signals independently auditable via
``rank_components``. To make room, directness/quality were trimmed by 2 points
each (35->33, 30->28); the six weights still sum to 1.0 and DIRECTNESS + QUALITY
remain dominant at 61% combined. Venue never sinks a record (unknown = neutral 50).

Weights live in ``RANK_WEIGHTS`` so they are easy to tune. ``rank_components``
records each axis's contribution so the ordering is fully auditable, and
``rank_rationale`` gives a one-line explanation. Impact defaults to 0 when no
citation data is present (an OpenAlex/Semantic Scholar backfill fills
``citation_count``); it never penalizes a record, it only promotes well-cited
ones once the data exists.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Dict

from .journal import journal_reputation

RANK_WEIGHTS = {
    "directness": 0.33,
    "quality": 0.28,
    "relevance": 0.20,
    "recency": 0.10,
    "impact": 0.05,
    "venue": 0.04,
}

# How central the molecule is to the paper, by role. Direct interventions and
# readouts are most relevant to "what does the evidence say about molecule X".
_RELEVANCE_BY_ROLE = {
    "direct_intervention": 100,
    "biomarker_readout": 78,
    "pathway_component": 72,
    "clinical_tool_or_diagnostic": 55,
    "comparator_or_background_drug": 52,
    "tool_compound_or_positive_control": 48,
    "primary_topic_unclear_role": 45,
    "assay_or_detection": 35,
    "synthesis_or_production": 35,
    "background_or_unclear": 30,
    "environmental_or_material_use": 10,
}

_CURRENT_YEAR = datetime.utcnow().year


@dataclass
class Rank:
    rank_score: int
    rank_tier: str
    rank_components: str
    rank_rationale: str
    components: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("components", None)
        return d


def _int(value, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _relevance(evidence: dict) -> float:
    role = str(evidence.get("role_category", "") or "")
    base = _RELEVANCE_BY_ROLE.get(role, 40)
    # Nudge by the upstream relevance confidence when present.
    conf = str(evidence.get("relevance_confidence", "") or "").lower()
    if conf == "high":
        base = min(100, base + 5)
    elif conf == "low":
        base = max(0, base - 10)
    return float(base)


def _recency(evidence: dict) -> float:
    y = _int(str(evidence.get("pub_year", ""))[:4], 0)
    if not y:
        return 30.0  # unknown year -> middling, not penalized to zero
    span = max(1, _CURRENT_YEAR - 2000)
    return max(0.0, min(100.0, (y - 2000) / span * 100.0))


def _impact(evidence: dict) -> float:
    """0-100 impact, preferring NIH iCite's field/time-normalized metrics over a
    raw count (which is unfair to newer work and to smaller fields):

      1) ``icite_nih_percentile`` (0-100) used directly -- 90th percentile -> 90.
      2) ``icite_rcr`` (Relative Citation Ratio; 1.0 = field median) log-scaled so
         1.0 -> 50, ~10 -> ~100, 0.1 -> 0.
      3) raw citation_count (iCite's, else OpenAlex's) log-scaled -- old fallback,
         for papers iCite has not scored.

    Never negative; 0 when nothing is available.
    """
    pct = evidence.get("icite_nih_percentile")
    if pct not in (None, ""):
        try:
            return max(0.0, min(100.0, float(pct)))
        except (TypeError, ValueError):
            pass
    rcr = evidence.get("icite_rcr")
    if rcr not in (None, ""):
        try:
            r = float(rcr)
            if r > 0:
                return max(0.0, min(100.0, 50.0 + 50.0 * math.log10(r)))
        except (TypeError, ValueError):
            pass
    cites = evidence.get("icite_citation_count") or evidence.get("citation_count")
    if cites in (None, ""):
        return 0.0
    m = re.search(r"\d+", str(cites))
    if not m:
        return 0.0
    n = int(m.group(0))
    if n <= 0:
        return 0.0
    return min(100.0, math.log10(n + 1) / 3.0 * 100.0)


def _venue(evidence: dict) -> float:
    """0-100 journal-reputation score.

    Prefers an already-computed ``journal_reputation`` field (set by the build so
    the value is emitted as a column); otherwise computes it on the fly from the
    ``journal`` name. Unknown/blank journals get the neutral ~50 default, so this
    axis never sinks a record.
    """
    pre = evidence.get("journal_reputation")
    if pre not in (None, ""):
        try:
            return float(int(str(pre).strip()))
        except (TypeError, ValueError):
            pass
    return float(journal_reputation(str(evidence.get("journal", "") or "")).journal_reputation)


def _tier(score: int) -> str:
    if score >= 70:
        return "high"
    if score >= 50:
        return "moderate"
    if score >= 30:
        return "limited"
    return "low"


def compute_rank(evidence: dict) -> Rank:
    """Blend the axes into a single 0-100 ranking score.

    Reads fields produced upstream: ``evidence_directness`` and
    ``reliability_score`` (from reliability.py), plus role/year/citations.
    Off-topic records (directness 0 and excluded) naturally sink to the bottom.
    """
    axes = {
        "directness": float(_int(evidence.get("evidence_directness"), 0)),
        "quality": float(_int(evidence.get("reliability_score"), 0)),
        "relevance": _relevance(evidence),
        "recency": _recency(evidence),
        "impact": _impact(evidence),
        "venue": _venue(evidence),
    }
    contributions = {k: round(RANK_WEIGHTS[k] * v, 2) for k, v in axes.items()}
    score = int(round(sum(contributions.values())))
    score = max(0, min(100, score))

    top = sorted(contributions.items(), key=lambda kv: kv[1], reverse=True)
    rationale = "rank = " + " + ".join(f"{k} {v:g}" for k, v in top if v > 0)
    return Rank(
        rank_score=score,
        rank_tier=_tier(score),
        rank_components=json.dumps({k: round(v, 1) for k, v in axes.items()}),
        rank_rationale=rationale,
        components=contributions,
    )


RANK_FIELDS = ["rank_score", "rank_tier", "rank_components", "rank_rationale"]
