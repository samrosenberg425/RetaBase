from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from .pubmed import PubMedRecord


@dataclass
class PaperSummary:
    evidence_summary: str = "not reported"
    key_result_sentence: str = "not reported"
    safety_signal_sentence: str = "not reported"
    summary_source: str = "abstract_only_rule_based"
    summary_needs_review: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


class RuleBasedEvidenceSummaryAgent:
    name = "rule_based_evidence_summary"

    def summarize(self, record: PubMedRecord, *, molecule_name: str = "") -> PaperSummary:
        sentences = _sentences(record.abstract)
        key_result = _find_sentence(
            sentences,
            ["result", "finding", "significant", "improved", "reduced", "increased", "decreased", "effective"],
        )
        safety = _find_sentence(sentences, ["safety", "adverse", "tolerability", "toxicity", "hypoglycaemia", "death"])
        opening = sentences[0] if sentences else record.title or "not reported"
        summary = _compose_summary(record, molecule_name, key_result or opening)
        return PaperSummary(
            evidence_summary=summary,
            key_result_sentence=key_result or "not reported",
            safety_signal_sentence=safety or "not reported",
            summary_source="abstract_only_rule_based",
            summary_needs_review=True,
        )


class NoOpSummaryAgent:
    name = "off"

    def summarize(self, record: PubMedRecord, *, molecule_name: str = "") -> PaperSummary:
        return PaperSummary(summary_source="not_generated", summary_needs_review=True)


def make_summary_agent(mode: str):
    mode = (mode or "rule_based").strip().lower()
    if mode == "off":
        return NoOpSummaryAgent()
    if mode in {"auto", "heuristic", "rule_based", "evidence", "summary"}:
        return RuleBasedEvidenceSummaryAgent()
    raise ValueError(f"Unsupported summary mode: {mode}")


def _compose_summary(record: PubMedRecord, molecule_name: str, sentence: str) -> str:
    pubtypes = ", ".join(record.pubtypes[:3]) if record.pubtypes else "PubMed record"
    date = record.pub_year or record.pub_date_iso or "undated"
    molecule = molecule_name or "the matched molecule"
    sentence = sentence[:450] if sentence else "not reported"
    return f"{date} {pubtypes}: {molecule}. {sentence}"


def _sentences(text: str) -> list:
    text = " ".join((text or "").split())
    if not text:
        return []
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def _find_sentence(sentences: list, terms: list) -> str:
    for sentence in sentences:
        lower = sentence.lower()
        if any(term in lower for term in terms):
            return sentence[:700]
    return ""
