"""Rule-based journal reputation signal.

A modest, *auditable* venue signal for the combined ranking. It is intentionally
conservative: it recognizes a small curated table of high-reputation biomedical
venues (and a few low/predatory signals) and **defaults to neutral (~50) for any
journal it does not recognize** — an unknown journal is never punished to 0.

Design principles (documented so the score is defensible):

* **Curated allowlist, not a live metric.** We do not fetch impact factors or
  SJR (that would need network + licensing). Instead we hand-maintain a tiered
  table of venues that a biomedical reader would recognize as top-tier or strong.
  This is transparent and offline; anyone can read/extend ``_VENUE_TIERS``.
* **Family matching.** "Nature Medicine", "Nature Metabolism", "Cell Metabolism",
  "The Lancet Diabetes & Endocrinology" etc. are matched by family patterns so we
  don't have to enumerate every sub-journal.
* **Neutral default.** Unknown venue -> 50 (``standard`` tier). Reputation is a
  small tie-breaker, not a gate; directness/quality stay dominant in ranking.
* **Low signals.** A few explicit low/predatory markers (e.g. "predatory",
  known low-quality patterns) drop below neutral, but we keep this list tiny to
  avoid mislabeling legitimate small journals.

Returns a 0-100 score, a tier string, and a one-line rationale. Rule-based,
offline, no network, no LLM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Tuple

# Score bands per tier. Kept coarse so the signal is a gentle nudge, not a cliff.
TIER_SCORES = {
    "flagship": 95,     # NEJM / Lancet / JAMA / Nature / Cell / Science flagships
    "top": 85,          # top-tier specialty (Nature/Cell family, Circulation, Diabetes Care...)
    "strong": 72,       # strong, well-indexed specialty journals
    "standard": 50,     # neutral default (unknown or ordinary indexed journal)
    "low": 35,          # weak signal (thin/obscure indexing)
    "predatory": 20,    # explicit predatory / very-low-quality markers
}

TIER_LABELS = {
    "flagship": "Flagship general medical/science",
    "top": "Top-tier specialty",
    "strong": "Strong specialty",
    "standard": "Standard / unrecognized",
    "low": "Low signal",
    "predatory": "Predatory / very low",
}

# Curated, auditable venue table. Each entry is (tier, [regex patterns]). Patterns
# are matched case-insensitively with word-ish boundaries against the normalized
# journal name. Order matters: the FIRST matching tier wins, so more specific /
# higher tiers are listed before broader family patterns.
_VENUE_TIERS: List[Tuple[str, List[str]]] = [
    # --- flagship general medicine & science ---
    ("flagship", [
        r"new england journal of medicine", r"\bn engl j med\b", r"\bnejm\b",
        r"\bthe lancet\b", r"^lancet$",
        r"\bjama\b", r"journal of the american medical association",
        r"^nature$", r"^science$", r"^cell$",
        r"\bbmj\b", r"british medical journal",
        r"annals of internal medicine",
        r"nature medicine",
    ]),
    # --- top-tier specialty (recognized, high-reputation venues) ---
    ("top", [
        # Nature / Cell / Science families (sub-journals)
        r"nature \w+", r"cell \w+", r"science \w+",
        r"\bcell metabolism\b", r"\bcell reports\b",
        # cardiovascular
        r"\bcirculation\b", r"european heart journal", r"journal of the american college of cardiology",
        r"\bjacc\b",
        # diabetes / endocrine / metabolism
        r"diabetes care", r"\bdiabetologia\b", r"lancet diabetes", r"nature reviews endocrinology",
        r"the lancet diabetes", r"journal of clinical endocrinology and metabolism",
        # hepatology / GI
        r"\bhepatology\b", r"\bgastroenterology\b", r"^gut$", r"journal of hepatology",
        # oncology
        r"journal of clinical oncology", r"lancet oncology", r"nature reviews cancer", r"cancer cell",
        # neurology / immunology
        r"lancet neurology", r"nature reviews", r"\bimmunity\b", r"nature immunology",
        # kidney
        r"journal of the american society of nephrology", r"kidney international",
        # broad high-impact multidisciplinary
        r"^pnas$", r"proceedings of the national academy of sciences",
    ]),
    # --- strong, well-indexed specialty ---
    ("strong", [
        r"\bcirculation research\b", r"american journal of \w+",
        r"journal of clinical investigation", r"^jci\b",
        r"\bdiabetes\b", r"\bobesity\b", r"international journal of obesity",
        r"molecular metabolism", r"\bmetabolism\b",
        r"\bendocrinology\b", r"journal of endocrinology",
        r"aging cell", r"\bgeroscience\b", r"\baging\b \(albany",
        r"\bebiomedicine\b", r"\bplos medicine\b",
        r"\bhypertension\b", r"stroke\b",
        r"british journal of pharmacology", r"pharmacological",
        r"journal of cachexia",
        # indexed systematic-review venue
        r"cochrane database of systematic reviews",
    ]),
    # --- low / predatory signals (kept intentionally tiny) ---
    ("predatory", [
        r"predatory", r"omics international", r"\bimedpub\b",
    ]),
]

# A few text markers that upgrade the confidence of a review/synthesis venue even
# if the journal itself is unrecognized (Cochrane is authoritative regardless).
_COCHRANE = re.compile(r"cochrane", re.IGNORECASE)

_MISSING = {"", "na", "n/a", "none", "null", "nan", "not reported", "unclear", "unknown", "nr"}


@dataclass
class JournalReputation:
    journal_reputation: int
    journal_tier: str
    journal_rationale: str


def _normalize(name: str) -> str:
    n = re.sub(r"\s+", " ", str(name or "")).strip().lower()
    # strip a trailing " : " subtitle noise and common punctuation
    n = n.replace("&", "and")
    n = re.sub(r"[.]", "", n)
    return n


def journal_reputation(journal_name: str) -> JournalReputation:
    """Return a 0-100 reputation score + tier + rationale for a journal name.

    Unknown or blank -> neutral ``standard`` tier (score 50); never punished to 0.
    """
    raw = str(journal_name or "").strip()
    norm = _normalize(raw)
    if not norm or norm in _MISSING:
        return JournalReputation(
            journal_reputation=TIER_SCORES["standard"],
            journal_tier="standard",
            journal_rationale="No journal name; neutral default (not penalized).",
        )

    for tier, patterns in _VENUE_TIERS:
        for pat in patterns:
            if re.search(pat, norm, re.IGNORECASE):
                return JournalReputation(
                    journal_reputation=TIER_SCORES[tier],
                    journal_tier=tier,
                    journal_rationale=f"{TIER_LABELS[tier]}: matched curated venue pattern '{pat}'.",
                )

    if _COCHRANE.search(norm):
        return JournalReputation(
            journal_reputation=TIER_SCORES["strong"],
            journal_tier="strong",
            journal_rationale="Cochrane systematic-review venue (authoritative synthesis).",
        )

    return JournalReputation(
        journal_reputation=TIER_SCORES["standard"],
        journal_tier="standard",
        journal_rationale="Unrecognized journal; neutral default (unknown is not penalized).",
    )


# Fields this module contributes to the curated schema.
JOURNAL_FIELDS = ["journal_reputation", "journal_tier", "journal_rationale"]
