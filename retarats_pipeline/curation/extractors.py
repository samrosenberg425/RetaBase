"""Additive extraction refinement + model disambiguation.

This module strengthens the two weakest spots of the upstream extraction:

* **Structured detail parsing** — dose, route, duration, and sample size are
  re-parsed from the paper's title + abstract with tight regexes so downstream
  filters have precise, machine-comparable values instead of the (sometimes
  noisy or absent) upstream free-text fields.
* **Model disambiguation** — records with *mixed* human/animal/in-vitro signals
  (≈2.2k of the 14.9k records carry two or more model flags) get a proposed,
  auditable primary-model classification that resolves the conflict with an
  explicit reason.

Everything here is **additive and non-destructive**: ``refine_extraction``
returns brand-new column names (all prefixed ``refined_`` or ``model_``) and
never overwrites an existing field. In particular ``model_primary`` is a
*parallel* proposal — the upstream ``model_type`` field is left untouched so a
reviewer can compare the two. Every decision records a ``*_reason`` /
``*_confidence`` so the classification is explainable from the data alone.

Pure regex + structured-field logic; no LLM, no network.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

# Values that mean "the upstream extractor found nothing" — treated as blank.
_MISSING = {
    "",
    "na",
    "n/a",
    "none",
    "null",
    "nan",
    "not reported",
    "not clearly reported",
    "unclear",
    "unknown",
    "not applicable",
    "nr",
}

# --- dose / route / duration / sample-size regexes -------------------------

# A dose: a number (optionally decimal or a fraction) followed by a unit,
# optionally with a per-kg / per-day compound (e.g. "2 mg/kg/day"). Anchored on
# word boundaries; matched case-insensitively against the raw text.
# NOTE: "kg" is deliberately NOT a standalone dose unit. A real drug dose is
# expressed in mg/µg/g or *per* kilogram (mg/kg); a bare "9 kg" is body weight
# or a BMI fragment (e.g. "39·9 kg/m²"), never a dose. "kg" is still honored as
# the denominator of a per-weight compound (the "/kg" group below).
# The number also accepts a middle-dot decimal (U+00B7), which journals such as
# The Lancet use in place of a period ("39·9", "1·25 mg") -- without this the
# regex would split "39·9" and pick up the trailing "9 kg" as a phantom dose.
# Molar amounts (nmol/mmol/...) ARE valid doses as a bare amount ("300 nmol
# bolus"), but the SAME units with a volume denominator ("7.2 mmol/L", "140 mg/dL",
# "5 mg/mL") are lab READOUTS / concentrations, not doses. So we keep the units but
# reject a match whose denominator is a concentration volume (see the lookahead).
# Per-body-weight ("/kg") stays a valid dose compound.
_DOSE_UNIT = r"(?:mg|µg|μg|ug|mcg|ng|g|IU|U|nmol|pmol|µmol|umol|mmol|mol|mL|ml|L)"
_DOSE_RE = re.compile(
    r"(?<![A-Za-z0-9.·])"
    r"\d+(?:[.·]\d+)?"
    r"\s*"
    rf"{_DOSE_UNIT}"
    r"(?:\s*/\s*(?:kg|g|day|d|dose|wk|week))*"
    # reject BMI "kg/m²" and concentration denominators "/L", "/dL", "/mL".
    r"(?!\s*/\s*(?:m[²2]|d?[lL]|mL|ml))"
    r"(?![A-Za-z])",
    re.IGNORECASE,
)

# Route of administration. Ordered so the normalized label is deterministic.
_ROUTE_PATTERNS: Tuple[Tuple[str, "re.Pattern"], ...] = tuple(
    (label, re.compile(pattern, re.IGNORECASE))
    for label, pattern in (
        ("oral", r"\b(?:oral(?:ly)?|per\s*os|by\s+mouth|gavage|orogastric)\b"),
        ("subcutaneous", r"\b(?:subcutaneous(?:ly)?|sub-?cutaneous(?:ly)?|s\.?c\.?)\b"),
        ("intravenous", r"\b(?:intravenous(?:ly)?|i\.?v\.?)\b"),
        ("intraperitoneal", r"\b(?:intraperitoneal(?:ly)?|i\.?p\.?)\b"),
        ("intramuscular", r"\b(?:intramuscular(?:ly)?|i\.?m\.?)\b"),
        ("intranasal", r"\b(?:intranasal(?:ly)?|nasal(?:ly)?)\b"),
        ("topical", r"\b(?:topical(?:ly)?|cutaneous(?:ly)?|dermal(?:ly)?)\b"),
        ("inhaled", r"\b(?:inhaled|inhalation|nebuli[sz]ed|aerosol(?:i[sz]ed)?)\b"),
        ("infusion", r"\b(?:infusion|infused)\b"),
    )
)

# Duration: "12 weeks", "8-week", "for 6 months", "over 24 h".
_DURATION_UNIT = r"(?:hours?|hrs?|h|days?|d|weeks?|wk|wks|months?|mo|years?|yrs?|yr)"
_DURATION_RE = re.compile(
    r"(?<![A-Za-z0-9·])"
    r"\d+(?:[.·]\d+)?"
    r"\s*[-‐‑‒–—]?\s*"
    rf"{_DURATION_UNIT}"
    r"(?![A-Za-z])",
    re.IGNORECASE,
)

# Sentence context that makes a duration likely a *study* duration rather than a
# patient-age or disease-duration distractor.
_DURATION_CONTEXT = re.compile(
    r"\b(?:for|over|during|treat|treatment|follow[\s-]?up|period|dur(?:ing|ation)|"
    r"administer|dosed|weeks?|months?|trial|study)\b",
    re.IGNORECASE,
)
_AGE_DISTRACTOR = re.compile(
    r"\b(?:age|aged|years?\s+old|mean\s+age|median\s+age|disease\s+duration|"
    r"diabetes\s+duration|old(?:er)?\s+adults?)\b",
    re.IGNORECASE,
)

# Sample size: explicit "n=123", "N = 1,234", or "<num> patients/participants/...".
_N_EQUALS_RE = re.compile(r"\bn\s*=\s*(\d{1,3}(?:,\d{3})+|\d{1,7})", re.IGNORECASE)
_N_NOUN_RE = re.compile(
    r"\b(\d{1,3}(?:,\d{3})+|\d{1,6})\s+"
    r"(?:patients?|participants?|subjects?|adults?|volunteers?|individuals?|"
    r"men|women|mice|rats?|animals?|dogs?|monkeys?|macaques?)\b",
    re.IGNORECASE,
)
# Dose/lab context that means a nearby number is NOT a sample size. Units are
# word-bounded so we don't match inside ordinary words (e.g. "ug" inside "drug",
# which previously discarded real sample sizes sitting next to the word "drug").
_N_EXCLUDE = re.compile(
    r"(?:\b(?:mg|µg|μg|ug|mcg|kg|IU|nmol|mmol|CFU|HbA1c)\b|×\s*10|x\s*10|/\s*kg|%)",
    re.IGNORECASE,
)

# --- outcome-direction sentiment -------------------------------------------

_BENEFICIAL = (
    "improved", "improvement", "reduced", "reduction", "decreased", "lowered",
    "attenuated", "ameliorated", "protected", "prevented", "restored",
    "enhanced", "increased survival", "weight loss", "superior", "efficacious",
    "benefit", "beneficial", "significantly better",
)
# NOTE: bare "death"/"mortality" are intentionally NOT here -- they are
# direction-ambiguous ("reduced mortality" is beneficial). Directional phrases
# carry the harm signal instead.
_HARMFUL = (
    "worsened", "aggravated", "exacerbated", "increased risk", "toxic",
    "toxicity", "serious adverse", "harmful", "impaired", "deleterious",
    "adverse outcome", "increased mortality", "higher mortality",
    "excess mortality", "increased death",
)
_NEUTRAL = (
    "no significant", "not significant", "no difference", "did not improve",
    "did not differ", "failed to", "no effect", "no clear", "unchanged",
    "did not reduce", "no benefit", "no reduction", "no change",
    "no improvement", "no association", "not associated",
)

# --- animal species (explicit non-human) ------------------------------------

_ANIMAL_SPECIES = re.compile(
    r"\b(?:mouse|mice|murine|C57BL|BALB|rat|rats|Sprague[- ]?Dawley|Wistar|Zucker|"
    r"porcine|swine|minipig|pigs?|canine|dogs?|beagle|rabbits?|leporine|"
    r"zebrafish|danio\s+rerio|drosophila|fruit\s+fly|caenorhabditis|c\.?\s*elegans|"
    r"monkey|monkeys|macaque|macaques|cynomolgus|rhesus|marmoset|baboon|"
    r"non-?human\s+primate|nonhuman\s+primate|sheep|ovine|bovine|guinea\s+pig|"
    r"hamster|ferret|primates?)\b",
    re.IGNORECASE,
)

# In-vitro / cell-only signals.
_IN_VITRO = re.compile(
    r"\b(?:in\s+vitro|cell\s+line|cell\s+culture|cultured\s+cells|HEK293|HepG2|"
    r"C2C12|3T3|SH-SY5Y|HeLa|RAW\s*264\.7|organoids?|myotubes?|spheroids?|"
    r"primary\s+cells|cell-?free)\b",
    re.IGNORECASE,
)

# Human population signals from free text. Deliberately excludes a bare
# "human(s)" token: phrases like "human recombinant protein" or "human GDF15"
# are reagent/gene references in animal or in-vitro studies, not evidence of a
# human study population. We require a population noun (patients, participants,
# "human subjects", etc.) so the human signal reflects who was actually studied.
_HUMAN_TEXT = re.compile(
    r"\b(?:patients?|participants?|volunteers?|human\s+subjects?|"
    r"men\s+and\s+women|adults?\s+with|clinical\s+trial|inpatients?|outpatients?)\b",
    re.IGNORECASE,
)

# Clinical study types => strong human signal even if cell work is mentioned.
_CLINICAL_STUDY_TYPES = (
    "rct", "randomized controlled trial", "human interventional",
    "human observational", "meta-analysis", "systematic review",
    "clinical trial", "cohort", "case-control", "cross-sectional",
    "phase 1", "phase 2", "phase 3", "phase 4", "phase i", "phase ii",
    "phase iii", "phase iv",
)

# Review study types.
_REVIEW_STUDY_TYPES = ("review", "meta-analysis", "systematic review")


def _is_missing(value) -> bool:
    return str(value or "").strip().lower() in _MISSING


def _flag(evidence: dict, key: str) -> bool:
    """Read a model flag robustly (booleans, "True"/"true"/1 all count)."""
    v = evidence.get(key)
    if v is True:
        return True
    return str(v).strip().lower() in {"true", "1", "yes"}


def _text_blob(evidence: dict, paper: dict) -> str:
    parts = [
        str(paper.get("title", "") or evidence.get("title", "") or ""),
        str(paper.get("abstract", "") or evidence.get("abstract", "") or ""),
    ]
    return " ".join(p for p in parts if p)


def _mesh_blob(paper: dict) -> str:
    mesh = paper.get("mesh_terms", "")
    if isinstance(mesh, (list, tuple)):
        return " ".join(str(x) for x in mesh)
    return str(mesh or "")


def _sentences(text: str) -> List[str]:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9([])", text) if s.strip()]


def _dedupe(items) -> List[str]:
    seen = set()
    out: List[str] = []
    for item in items:
        key = re.sub(r"\s+", "", item.lower())
        if key and key not in seen:
            seen.add(key)
            out.append(item)
    return out


# Off-focus noise the search query can't catch: opinion / infodemiology studies
# (public perception of a bioactive, social-media / Google-Trends analyses, media
# coverage). These can legitimately carry the molecule in the title, so title/MeSH
# anchoring won't drop them -- but they are about DISCOURSE, not therapeutic use or
# mechanism, so they don't belong in the evidence base. Deliberately narrow: only
# unambiguous opinion/infodemiology signals, so real clinical/mechanistic work is
# left untouched.
_OFF_FOCUS_OPINION = re.compile(
    r"\b(social media|twitter|reddit|tiktok|instagram|facebook|youtube|"
    r"google trends|infodemiolog\w*|sentiment analysis|"
    r"public (?:opinion|perception|perceptions|attitude|attitudes|awareness)|"
    r"online (?:forum|forums|discourse|posts?|content|communities)|"
    r"misinformation|news coverage|media coverage|web search(?:es)?)\b",
    re.IGNORECASE,
)


def off_focus_reason(evidence: dict, paper: dict) -> str:
    """Reason string if the paper is off-focus noise (opinion / infodemiology),
    else "". Routes such records to excluded_noise. Narrow by design so it never
    catches genuine therapeutic-use or mechanistic studies."""
    if _OFF_FOCUS_OPINION.search(_text_blob(evidence, paper)):
        return "opinion / infodemiology study (public discourse, not therapeutic use or mechanism)"
    return ""


# --- public field-level parsers --------------------------------------------


def _dose_list(text: str) -> List[str]:
    """Raw dose expressions in ``text`` (order-preserving, not de-duped)."""
    return [re.sub(r"\s+", " ", m.group(0)).strip() for m in _DOSE_RE.finditer(text)]


def parse_dose(text: str) -> str:
    """Return a semicolon list of distinct dose expressions found in ``text``."""
    return "; ".join(_dedupe(_dose_list(text))[:6])


def parse_route(text: str) -> str:
    """Return a semicolon list of normalized routes of administration."""
    hits: List[str] = []
    for label, pattern in _ROUTE_PATTERNS:
        if pattern.search(text):
            hits.append(label)
    return "; ".join(hits)


def parse_duration(text: str) -> str:
    """Return a semicolon list of study-duration expressions.

    Only durations appearing in a study/treatment context (and not next to an
    age/disease-duration distractor) are kept, to avoid picking up patient ages.
    """
    out: List[str] = []
    for sent in _sentences(text):
        if not _DURATION_CONTEXT.search(sent):
            continue
        for m in _DURATION_RE.finditer(sent):
            # Keep the distractor window LOCAL to the number: an age cue only
            # disqualifies the match if it sits right next to it (e.g. "aged 18
            # years"), not somewhere else in the sentence. A wide window wrongly
            # blanked real study durations whenever an age was mentioned anywhere
            # in the same clause ("for 12 weeks in patients aged 18 years").
            window = sent[max(0, m.start() - 12): m.end() + 6]
            if _AGE_DISTRACTOR.search(window):
                continue
            out.append(re.sub(r"\s+", " ", m.group(0)).strip())
    return "; ".join(_dedupe(out)[:6])


def parse_sample_size(text: str) -> Tuple[str, Optional[int]]:
    """Return (display_string, best_int_N).

    Prefers explicit ``n=`` counts, then ``<num> patients``-style counts. When
    several arm-level counts are present and no total is given, the arms are
    summed as a best-effort total (recorded in the display string).
    """
    n_equals: List[int] = []
    for m in _N_EQUALS_RE.finditer(text):
        n_equals.append(int(m.group(1).replace(",", "")))

    noun_counts: List[int] = []
    for m in _N_NOUN_RE.finditer(text):
        window = text[max(0, m.start() - 30): m.end() + 15]
        if _N_EXCLUDE.search(window):
            continue
        noun_counts.append(int(m.group(1).replace(",", "")))

    # A "total"/"overall" cue means one of the numbers is the stated total, so we
    # must NOT sum (e.g. "60 mice total" alongside "20 mice per group").
    has_total_cue = bool(re.search(r"\b(?:total|overall|altogether|in all)\b", text, re.IGNORECASE))
    # Only SUM several counts when there is an explicit multi-arm join cue. Without
    # one, distinct counts are usually the SAME cohort reported at different stages
    # ("n=50 enrolled ... n=48 analyzed") -> summing them (98) is wrong, so we take
    # the max. This mirrors the audit's "prefer max unless an explicit join".
    has_arm_cue = bool(re.search(
        r"\b(?:per\s+group|per\s+arm|each\s+group|each\s+arm|in\s+each|"
        r"randomi[sz]ed\s+to|allocated\s+to|assigned\s+to|vs\.?|versus|arms?|groups?)\b",
        text, re.IGNORECASE))
    # ...but a COHORT-FLOW cue means the counts are one cohort at different stages
    # (enrolled/screened/analyzed/completed/withdrew/ITT), which must NOT be summed
    # even if a word like "groups" also appears. Flow wins -> take the max.
    # Cohort-STAGE words (no trailing \b, so "enroll" catches "enrolled" etc.).
    # Deliberately excludes "randomized" -- a genuine 2-arm RCT ("randomized to
    # drug n=50 or placebo n=48") SHOULD sum; these are same-cohort flow stages.
    has_flow_cue = bool(re.search(
        r"\b(?:enroll|screen|analy[sz]|complet|withdr[ae]w|"
        r"discontinu|drop(?:ped)?\s*-?\s*out|lost\s+to\s+follow|per[- ]protocol|"
        r"intention[- ]to[- ]treat|\bITT\b|evaluable|included\s+in\s+the\s+analysis)",
        text, re.IGNORECASE))
    allow_sum = has_arm_cue and not has_flow_cue

    if n_equals:
        best, display = _resolve_counts(n_equals, prefix="n=", has_total_cue=has_total_cue, allow_sum=allow_sum)
    elif noun_counts:
        best, display = _resolve_counts(noun_counts, prefix="", has_total_cue=has_total_cue, allow_sum=allow_sum)
    else:
        return "", None
    return display, best


def _resolve_counts(counts: List[int], prefix: str, has_total_cue: bool,
                    allow_sum: bool = False) -> Tuple[int, str]:
    """Resolve several reported counts into a single best-effort N.

    Correctness guard: distinct counts are only SUMMED when the text shows an
    explicit multi-arm join (``allow_sum``, e.g. "per group", "vs", "arms"). We
    take the largest value as the total (and do NOT sum) when:

    * there is no arm-join cue (``allow_sum`` False) -- distinct counts are then
      usually the SAME cohort at different stages ("n=50 enrolled ... n=48
      analyzed"), where summing to 98 would be wrong, or
    * the text carries a "total"/"overall" cue, or
    * the largest value exactly equals the sum of the rest (the "n=100 = 50+50"
      total signature).

    Only with an arm-join cue and none of the above do we sum — recording that we did.
    """
    m = max(counts)
    others = list(counts)
    others.remove(m)
    if not others or has_total_cue or m == sum(others) or not allow_sum:
        return m, f"{prefix}{m}"
    arm_sum = sum(counts)
    arms = ", ".join(str(x) for x in counts)
    return arm_sum, f"{prefix}{arm_sum} (sum of arms {arms})"


# Negation cues that flip an outcome term to "no effect" when they sit just before
# it -- so "no reduction in mortality" / "did not improve" aren't read as signals.
_OUTCOME_NEG = (
    "no ", "not ", "n't", "without", "failed to", "absence of", "lack of",
    "did not", "does not", "were not", "was not", "neither", "nor ", "non-",
)


def _has_unnegated(text: str, terms) -> bool:
    """True if any term appears NOT locally preceded by a negation cue."""
    for t in terms:
        start = 0
        while True:
            i = text.find(t, start)
            if i == -1:
                break
            if not any(neg in text[max(0, i - 20):i] for neg in _OUTCOME_NEG):
                return True
            start = i + len(t)
    return False


def classify_outcome(evidence: dict, text: str) -> str:
    """beneficial | harmful | neutral | unclear from efficacy/safety sentences.

    Improves on the upstream ``outcome_direction`` by reading the efficacy and
    safety signal sentences plus abstract text. Matching is negation-aware so a
    negated cue ("no reduction in mortality", "did not improve") is not counted as
    a positive/negative signal, and bare "mortality"/"death" (direction-ambiguous)
    only signal harm via explicit directional phrases ("increased mortality").
    """
    eff = str(evidence.get("efficacy_signal", "") or "").lower()
    safe = str(evidence.get("safety_signal", "") or "").lower()
    upstream = str(evidence.get("outcome_direction", "") or "").lower()
    blob = f"{eff} {safe} {text.lower()}"

    # Upstream already gives a clean signal in many cases; respect it first.
    if "beneficial" in upstream or "desired" in upstream:
        return "beneficial"
    if "harmful" in upstream:
        return "harmful"
    if "neutral" in upstream or "no clear" in upstream:
        return "neutral"

    # Explicit no-effect statements -> neutral.
    if any(t in blob for t in _NEUTRAL):
        return "neutral"
    # Inherent-harm safety cues (negation-aware).
    if _has_unnegated(safe, ("serious adverse", "toxicity")) or _has_unnegated(blob, _HARMFUL):
        return "harmful"
    if _has_unnegated(blob, _BENEFICIAL):
        return "beneficial"
    return "unclear"


# --- model disambiguation ---------------------------------------------------


def _collect_model_signals(evidence: dict, paper: dict) -> Tuple[List[str], dict]:
    """Return (ordered signal list, detail dict) of all model signals present."""
    blob = _text_blob(evidence, paper)
    mesh = _mesh_blob(paper)
    species = str(evidence.get("species_or_population", "") or "").lower()
    primary = str(evidence.get("primary_study_type", "") or "").lower()

    signals: List[str] = []
    detail = {
        "human_flag": _flag(evidence, "human_flag"),
        "animal_flag": _flag(evidence, "animal_flag"),
        "in_vitro_flag": _flag(evidence, "in_vitro_flag"),
        "clinical_study_type": any(t in primary for t in _CLINICAL_STUDY_TYPES),
        "review_study_type": any(t in primary for t in _REVIEW_STUDY_TYPES),
        "human_text": bool(_HUMAN_TEXT.search(blob)),
        "human_species_field": any(t in species for t in ("patient", "human", "participant", "volunteer")),
        "animal_text": bool(_ANIMAL_SPECIES.search(blob) or _ANIMAL_SPECIES.search(mesh)),
        "in_vitro_text": bool(_IN_VITRO.search(blob)),
    }

    if detail["human_flag"] or detail["human_text"] or detail["human_species_field"] or detail["clinical_study_type"]:
        signals.append("human")
    if detail["animal_flag"] or detail["animal_text"]:
        signals.append("animal")
    if detail["in_vitro_flag"] or detail["in_vitro_text"]:
        signals.append("in_vitro")
    if detail["review_study_type"]:
        signals.append("review")
    return signals, detail


def disambiguate_model(evidence: dict, paper: dict) -> Tuple[str, str, str, str]:
    """Resolve mixed model signals into a single proposed primary classification.

    Returns ``(model_primary, model_flags, model_confidence, reason)`` where:

    * ``model_primary`` is one of human | animal | in_vitro | review | unclear
    * ``model_flags``   is a semicolon list of ALL model signals present
    * ``model_confidence`` is high | medium | low
    * ``reason``        is a short human-readable justification

    Rule of thumb: a clinical study type or explicit human population wins over
    incidental cell/animal mentions (clinical papers routinely describe cell
    sub-studies); an explicit animal species wins over a bare in-vitro mention;
    only-in-vitro signals classify as in_vitro. This is a *parallel* proposal —
    it never overwrites ``model_type``.
    """
    signals, d = _collect_model_signals(evidence, paper)
    flags = "; ".join(signals)
    n = len(signals)

    strong_human = d["clinical_study_type"] or d["human_flag"] or d["human_species_field"]
    strong_animal = d["animal_flag"] or d["animal_text"]
    only_in_vitro = d["in_vitro_flag"] or d["in_vitro_text"]

    # Pure review with no primary data.
    if d["review_study_type"] and "human" not in signals and "animal" not in signals and "in_vitro" not in signals:
        return "review", flags or "review", "high", "review/synthesis study type with no primary model signal"

    # Human dominance: clinical design or explicit human population beats
    # incidental animal/cell mentions (common in clinical papers with sub-studies).
    if strong_human:
        if "animal" in signals or "in_vitro" in signals:
            conf = "medium" if (d["clinical_study_type"] or d["human_species_field"]) else "low"
            return (
                "human",
                flags,
                conf,
                "clinical/human population signal outweighs incidental "
                + " & ".join(s for s in ("animal", "in_vitro") if s in signals)
                + " mention(s)",
            )
        conf = "high" if (d["clinical_study_type"] and (d["human_flag"] or d["human_species_field"])) else "medium"
        return "human", flags, conf, "human study-type / population signal, no competing preclinical signal"

    # Explicit animal species (no human signal): animal wins over bare in-vitro.
    if strong_animal:
        if "in_vitro" in signals:
            return (
                "animal",
                flags,
                "medium",
                "explicit animal species signal outweighs in-vitro mention (likely in-vivo study with cell sub-experiments)",
            )
        conf = "high" if d["animal_flag"] and d["animal_text"] else "medium"
        return "animal", flags, conf, "explicit animal species / animal flag, no human signal"

    # Only in-vitro signals remain.
    if only_in_vitro:
        conf = "high" if d["in_vitro_flag"] and d["in_vitro_text"] else "medium"
        return "in_vitro", flags, conf, "only in-vitro / cell signals present"

    # Nothing resolvable.
    return "unclear", flags, "low", "no clear human/animal/in-vitro signal in structured fields or text"


# --- top-level entry point --------------------------------------------------


# --- molecule-scoped extraction --------------------------------------------
# TRUST FIX: in comparator / combination papers the abstract names several drugs
# and their doses. Extracting over the whole abstract would attach a comparator's
# dose/route/duration to THIS record's molecule (e.g. a semaglutide dose showing
# on a tirzepatide card). So when the abstract shows a comparison, we restrict
# dose/route/duration to sentences that mention the record's OWN molecule; and if
# even one such sentence still carries >=2 distinct doses (both drugs in a single
# clause -> not attributable by rules alone), we decline to guess and mark the
# dose "not localized". Single-drug papers are unaffected (whole-text, as before).
# The chosen scope is reported in ``refined_extraction_scope`` for provenance.
_COMPARATOR_CUE = re.compile(
    r"\b(?:versus|vs\.?|compared\s+(?:with|to|against)|head[\s-]?to[\s-]?head|"
    r"relative\s+to|non-?inferior(?:ity)?|superiority\s+(?:to|over)|"
    r"in\s+combination\s+with|combined\s+with|co-?administ|as\s+an?\s+add-?on)\b",
    re.IGNORECASE,
)


def _molecule_terms(evidence: dict, paper: dict) -> List[str]:
    """Lowercase name / id / synonym anchors for the record's molecule (len >= 3)."""
    raw = [
        str(evidence.get("molecule_name", "") or ""),
        str(evidence.get("molecule_id", "") or "").replace("_", " "),
    ]
    syn = evidence.get("molecule_synonyms") or paper.get("molecule_synonyms") or ""
    if isinstance(syn, (list, tuple)):
        raw.extend(str(s) for s in syn)
    else:
        raw.extend(str(syn).split(";"))
    terms = {t.strip().lower() for t in raw if len(t.strip()) >= 3}
    return sorted(terms)


def _mentions(sentence: str, terms: List[str]) -> bool:
    s = sentence.lower()
    return any(t in s for t in terms)


def refine_extraction(evidence: dict, paper: dict) -> dict:
    """Return additive refined-extraction + model-disambiguation fields.

    All returned keys are new column names (``refined_*`` / ``model_*`` except
    ``model_type``) so this never collides with or overwrites existing fields.
    Fall back to ``""`` (empty) when a value cannot be parsed.
    """
    text = _text_blob(evidence, paper)
    terms = _molecule_terms(evidence, paper)
    comparator = bool(terms) and bool(_COMPARATOR_CUE.search(text))

    if comparator:
        local = [s for s in _sentences(text) if _mentions(s, terms)]
        local_text = " ".join(local)
        # If a single molecule-local clause still carries two+ distinct doses, the
        # comparator's dose is entangled with ours -> don't guess.
        ambiguous = any(len(set(_dose_list(s))) >= 2 for s in local)
        if ambiguous:
            refined_dose = ""
            extraction_scope = "ambiguous_multidrug"
        else:
            refined_dose = parse_dose(local_text)
            extraction_scope = "molecule_local"
        refined_route = parse_route(local_text)
        refined_duration = parse_duration(local_text)
    else:
        refined_dose = parse_dose(text)
        refined_route = parse_route(text)
        refined_duration = parse_duration(text)
        extraction_scope = "document"

    # Sample size is about participants (one cohort), not per-drug, so it stays
    # document-wide regardless of scope.
    sample_display, sample_n = parse_sample_size(text)
    refined_outcome = classify_outcome(evidence, text)

    model_primary, model_flags, model_conf, model_reason = disambiguate_model(evidence, paper)

    return {
        "refined_dose": refined_dose,
        "refined_route": refined_route,
        "refined_duration": refined_duration,
        "refined_sample_size": sample_display,
        "refined_n": sample_n if sample_n is not None else "",
        "refined_outcome_direction": refined_outcome,
        "refined_extraction_scope": extraction_scope,
        "model_primary": model_primary,
        "model_flags": model_flags,
        "model_confidence": model_conf,
        "model_disambiguation_reason": model_reason,
    }


# Ordered list of the additive columns this module contributes, so the builder
# can splice them into the curated schema in a stable position.
REFINED_FIELDS = [
    "refined_dose",
    "refined_route",
    "refined_duration",
    "refined_sample_size",
    "refined_n",
    "refined_outcome_direction",
    "refined_extraction_scope",
    "model_primary",
    "model_flags",
    "model_confidence",
    "model_disambiguation_reason",
]
