#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from retarats_pipeline.enrichment.basic_science import enrich_basic_science
from retarats_pipeline.enrichment.common import APIConfig, ensure_dir, load_payload_table, save_payload_rows, write_csv
from retarats_pipeline.enrichment.human import enrich_human_interventions

EVIDENCE_EXPORT_PREFERRED = [
    "processing_lane", "molecule_id", "molecule_name", "public_candidate", "primary_study_type",
    "evidence_id", "pmid", "enriched_nct_id", "enriched_trial_match_confidence",
    "enriched_human_original_completeness", "enriched_human_proposed_completeness",
    "enriched_basic_original_completeness", "enriched_basic_proposed_completeness",
    "abstract_extraction_confidence", "pmc_enrichment_eligible", "pmc_enrichment_attempted",
    "pmc_enrichment_status", "pmcid", "pmc_enrichment_reason",
    "suggested_population_or_sample", "suggested_comparator_or_control", "suggested_dose_route",
    "suggested_duration", "suggested_sample_size", "suggested_model_system_detail",
    "suggested_mechanistic_focus", "suggested_condition_tags", "suggested_endpoint_tags",
    "suggested_dose_route_source", "suggested_model_system_detail_source",
    "suggested_mechanistic_focus_source", "suggested_endpoint_tags_source",
    "abstract_model_system_detail", "abstract_mechanistic_focus", "abstract_condition_tags",
    "abstract_endpoint_tags", "abstract_dose_route", "abstract_duration", "abstract_sample_size",
    "pmc_model_system_detail", "pmc_mechanistic_focus", "pmc_condition_tags",
    "pmc_endpoint_tags", "pmc_dose_route", "pmc_duration", "pmc_sample_size",
    "enriched_source_title", "enriched_source_abstract",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run RetaRats enrichment layer and write audit CSVs before destructive replacement.")
    parser.add_argument("--db", default="data/retarats_pubmed.sqlite")
    parser.add_argument("--out-dir", default="exports/enriched")
    parser.add_argument("--review-queue-dir", default="exports/review_queue")
    parser.add_argument("--mode", choices=["all", "human", "basic"], default="all")
    parser.add_argument("--offline", action="store_true", help="Disable external API calls; run heuristic/audit extraction only.")
    parser.add_argument("--csv-only", action="store_true", help="Do not write enriched_* fields back to SQLite payloads.")
    parser.add_argument("--max-records", type=int, default=0, help="Limit records per selected enrichment path for testing.")
    parser.add_argument("--enable-pmc", action="store_true", help="Enable live PMC full-text fallback for eligible incomplete basic-science rows.")
    parser.add_argument("--pmc-max-records", type=int, default=25, help="Maximum PMC full-text attempts in one run. Use 0 for no extra cap.")
    parser.add_argument("--contact-email", default="", help="Email for Crossref/Unpaywall polite API calls. Defaults to API_CONTACT_EMAIL or sr2007@rwjms.rutgers.edu.")
    parser.add_argument("--ncbi-email", default="", help="Email for NCBI calls. Defaults to NCBI_EMAIL or samrosenberg425@gmail.com.")
    parser.add_argument("--cache-dir", default="data/api_cache")
    args = parser.parse_args()

    config = APIConfig.from_env(api_enabled=not args.offline, cache_dir=args.cache_dir)
    if args.contact_email:
        config.contact_email = args.contact_email
    if args.ncbi_email:
        config.ncbi_email = args.ncbi_email
    config.user_agent = f"{config.tool_name}/0.1 (mailto:{config.contact_email})"

    ensure_dir(args.out_dir)
    ensure_dir(args.review_queue_dir)

    conn = sqlite3.connect(args.db)
    evidence_rows = load_payload_table(conn, "evidence")
    paper_rows = load_payload_table(conn, "papers")
    paper_by_pmid = {str(p.get("pmid", "")): p for p in paper_rows}

    all_updates: Dict[str, dict] = {}
    all_audits: List[dict] = []

    if args.mode in {"all", "human"}:
        human_updates, human_audit, ct_matches, registry_records = enrich_human_interventions(
            evidence_rows=evidence_rows,
            paper_by_pmid=paper_by_pmid,
            config=config,
            max_records=args.max_records,
        )
        for row in human_updates:
            all_updates[str(row.get("evidence_id", ""))] = row
        all_audits.extend({"audit_type": "human_intervention", **row} for row in human_audit)
        write_csv(Path(args.out_dir) / "human_intervention_enrichment_audit.csv", human_audit)
        write_csv(Path(args.out_dir) / "clinicaltrials_matches.csv", ct_matches)
        write_csv(Path(args.out_dir) / "clinicaltrials_registry_records.csv", registry_records)
        write_csv(Path(args.review_queue_dir) / "pico_incomplete.csv", [r for r in human_audit if str(r.get("needs_human_review", "")).lower() == "true"])

    if args.mode in {"all", "basic"}:
        basic_updates, basic_audit, annotations, pmc_audit = enrich_basic_science(
            evidence_rows=evidence_rows,
            paper_by_pmid=paper_by_pmid,
            config=config,
            max_records=args.max_records,
            enable_pmc=args.enable_pmc,
            pmc_max_records=args.pmc_max_records,
        )
        for row in basic_updates:
            all_updates[str(row.get("evidence_id", ""))] = row
        all_audits.extend({"audit_type": "basic_science", **row} for row in basic_audit)
        write_csv(Path(args.out_dir) / "basic_science_enrichment_audit.csv", basic_audit)
        write_csv(Path(args.out_dir) / "basic_science_annotations.csv", annotations)
        write_csv(Path(args.out_dir) / "pmc_full_text_audit.csv", pmc_audit)
        basic_review = [r for r in basic_audit if r.get("review_reason") and r.get("basic_priority") != "internal_methods_or_noise"]
        write_csv(Path(args.review_queue_dir) / "basic_science_incomplete.csv", basic_review)

    merged_updates = _merge_updates_into_all_rows(evidence_rows, all_updates)
    updated_subset = list(all_updates.values())
    write_csv(Path(args.out_dir) / "evidence_enriched_subset.csv", updated_subset, preferred=EVIDENCE_EXPORT_PREFERRED)
    write_csv(Path(args.out_dir) / "enrichment_audit_all.csv", all_audits)

    if not args.csv_only and all_updates:
        save_payload_rows(conn, "evidence", "evidence_id", merged_updates, updated_field="enriched_at_utc")
        print(f"updated SQLite evidence payloads with enriched_* fields: {len(all_updates)} rows")
    elif args.csv_only:
        print("csv-only mode: SQLite was not modified")
    else:
        print("no rows updated")
    conn.close()

    print(f"wrote enrichment outputs under {args.out_dir}")
    print(f"wrote review queues under {args.review_queue_dir}")
    if args.offline:
        print("offline mode: API calls were disabled; outputs are heuristic/audit-only")


def _merge_updates_into_all_rows(original_rows: List[dict], updates_by_evidence_id: Dict[str, dict]) -> List[dict]:
    if not updates_by_evidence_id:
        return original_rows
    out: List[dict] = []
    for row in original_rows:
        eid = str(row.get("evidence_id", ""))
        out.append(updates_by_evidence_id.get(eid, row))
    return out


if __name__ == "__main__":
    main()
