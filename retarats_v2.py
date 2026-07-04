#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args, **kwargs):
        return False

from retarats_pipeline.classifier import classify_record
from retarats_pipeline.config import load_config, molecule_lookup
from retarats_pipeline.paper_characterizer import characterize_paper
from retarats_pipeline.processing_router import route_evidence
from retarats_pipeline.profiles import build_molecule_profiles, load_evidence_payloads
from retarats_pipeline.pubmed import PubMedClient, PubMedRecord, utc_now_iso
from retarats_pipeline.relevance import classify_molecule_relevance
from retarats_pipeline.role_classifier import classify_role, load_role_rules
from retarats_pipeline.sinks import PipelineState, build_sinks
from retarats_pipeline.summarizers import make_summary_agent


def main() -> None:
    load_dotenv()
    args = parse_args()
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    gc = None
    if args.config_mode == "google":
        gc = google_client()

    loaded = load_config(
        mode=args.config_mode,
        local_config_dir=args.config_dir,
        google_sheet_name=args.config_sheet_name,
        input_workbook=args.input_workbook,
        summary_workbook=args.summary_workbook,
        gspread_client=gc,
    )
    molecules = molecule_lookup(loaded.molecules)
    rules = loaded.rules
    if args.molecule:
        rules = rules[rules["molecule_id"].astype(str) == args.molecule].copy()
    if args.max_rules:
        rules = rules.head(args.max_rules).copy()

    state = PipelineState(args.state_db)
    sinks = build_sinks(args.sinks, local_db=args.local_db, google_sheet_name=args.output_google_sheet)
    sinks.upsert_molecules(molecules.values())

    summary_agent = make_summary_agent(args.summary_mode)
    client = PubMedClient(
        email=os.getenv("NCBI_EMAIL", "").strip(),
        api_key=os.getenv("NCBI_API_KEY", "").strip(),
        tool=os.getenv("NCBI_TOOL", "retarats_pubmed_pipeline_v2"),
        max_requests_per_second=float(os.getenv("NCBI_MAX_RPS", "9.0" if os.getenv("NCBI_API_KEY") else "2.5")),
    )

    print(f"Run {run_id}: {len(rules)} active rules, summary agent={summary_agent.name}, sinks={args.sinks}", flush=True)
    role_rules = []
    if args.role_rules and Path(args.role_rules).exists():
        role_rules = load_role_rules(args.role_rules)
        print(f"Loaded {len(role_rules)} molecule-role rules from {args.role_rules}", flush=True)
    elif args.role_rules:
        print(f"Role rules file not found at {args.role_rules}; role fields will be blank.", flush=True)

    total_seen = total_new = total_written = 0
    new_evidence_for_profiles: List[dict] = []

    for _, rule in rules.iterrows():
        molecule_id = str(rule["molecule_id"]).strip()
        molecule = molecules.get(molecule_id, {})
        molecule_name = str(molecule.get("display_name") or molecule_id)
        rule_id = str(rule["rule_id"]).strip()
        query = str(rule["query_string"]).strip()
        match_strength = str(rule["match_strength"]).strip().lower()

        for window_name, search_kwargs in iter_search_windows(args):
            search = client.esearch(term=query, **search_kwargs)
            print(f"{molecule_id}/{rule_id}/{window_name}: {search.count} PubMed hits", flush=True)
            if search.count == 0:
                continue

            batch_papers: List[dict] = []
            batch_evidence: List[dict] = []
            rule_new = 0

            for record in client.iter_records_from_search(search, batch_size=args.pubmed_batch_size):
                total_seen += 1
                evidence_id = f"{record.pmid}:{molecule_id}:{rule_id}"
                if state.seen(evidence_id) and not args.refresh_seen:
                    continue

                paper, evidence = build_rows(
                    record=record,
                    molecule=molecule,
                    molecule_id=molecule_id,
                    molecule_name=molecule_name,
                    rule_id=rule_id,
                    match_strength=match_strength,
                    query=query,
                    run_id=run_id,
                    summary_agent=summary_agent,
                    role_rules=role_rules,
                )
                batch_papers.append(paper)
                batch_evidence.append(evidence)
                new_evidence_for_profiles.append(evidence)
                state.mark_seen(evidence_id, evidence["fetched_at_utc"])
                total_new += 1
                rule_new += 1

                if len(batch_evidence) >= args.write_batch_size:
                    sinks.upsert_papers(batch_papers)
                    sinks.upsert_evidence(batch_evidence)
                    total_written += len(batch_evidence)
                    print(f"  wrote {len(batch_evidence)} records; total_written={total_written}", flush=True)
                    batch_papers, batch_evidence = [], []

                if args.max_records_per_rule and rule_new >= args.max_records_per_rule:
                    break

            if batch_evidence:
                sinks.upsert_papers(batch_papers)
                sinks.upsert_evidence(batch_evidence)
                total_written += len(batch_evidence)
                print(f"  wrote {len(batch_evidence)} records; total_written={total_written}", flush=True)

    profile_rows = build_profiles_for_output(args, molecules, new_evidence_for_profiles, utc_now_iso())
    if profile_rows:
        sinks.upsert_molecule_profiles(profile_rows)
        print(f"updated {len(profile_rows)} molecule evidence profiles", flush=True)

    print(f"DONE: inspected={total_seen}, new_or_refreshed={total_new}, written={total_written}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="RetaRats PubMed pipeline v2: PubMed -> local/Google/Airtable")
    parser.add_argument("--config-mode", choices=["local", "google", "inputs", "excel", "xlsx"], default=os.getenv("CONFIG_MODE", "local"))
    parser.add_argument("--config-dir", default=os.getenv("LOCAL_CONFIG_DIR", "config"))
    parser.add_argument("--config-sheet-name", default=os.getenv("CONFIG_SHEET_NAME", "Moleculessearch"))
    parser.add_argument("--input-workbook", default=os.getenv("INPUT_WORKBOOK", "inputs/Moleculessearch.xlsx"))
    parser.add_argument("--summary-workbook", default=os.getenv("SUMMARY_WORKBOOK", "inputs/Summary Sheet.xlsx"))
    parser.add_argument("--mode", choices=["daily", "backfill"], default=os.getenv("V2_RUN_MODE", os.getenv("RUN_MODE", "daily")).lower())
    parser.add_argument("--start-year", type=int, default=int(os.getenv("V2_DEFAULT_START_YEAR", os.getenv("DEFAULT_START_YEAR", "2000"))))
    parser.add_argument("--end-year", type=int, default=int(os.getenv("V2_DEFAULT_END_YEAR", "0")), help="Optional upper year bound (0 = current year). Enables single-year backfill windows.")
    parser.add_argument("--daily-days", type=int, default=int(os.getenv("V2_DEFAULT_DAILY_DAYS", os.getenv("DEFAULT_DAILY_DAYS", "7"))))
    parser.add_argument("--datetype", default=os.getenv("NCBI_DATETYPE", "pdat"))
    parser.add_argument("--molecule", help="Optional molecule_id filter, e.g. retatrutide")
    parser.add_argument("--max-rules", type=int, default=0)
    parser.add_argument("--max-records-per-rule", type=int, default=0)
    parser.add_argument("--pubmed-batch-size", type=int, default=100)
    parser.add_argument("--write-batch-size", type=int, default=25)
    parser.add_argument("--summary-mode", choices=["off", "heuristic", "rule_based", "evidence", "auto"], default=os.getenv("SUMMARY_MODE", "rule_based"))
    parser.add_argument("--role-rules", default=os.getenv("ROLE_RULES", "config/ROLE_RULES.csv"), help="CSV rule file for molecule-role categorization")
    parser.add_argument("--sinks", default=os.getenv("OUTPUT_SINKS", "local"), help="Comma list: local,google,airtable")
    parser.add_argument("--local-db", default=os.getenv("LOCAL_DB", "data/retarats_pubmed.sqlite"))
    parser.add_argument("--state-db", default=os.getenv("STATE_DB", "data/retarats_state.sqlite"))
    parser.add_argument("--output-google-sheet", default=os.getenv("OUTPUT_GOOGLE_SHEET", "RetaRats_PubMed_v2"))
    parser.add_argument("--refresh-seen", action="store_true", help="Regenerate records even if evidence_id was seen before")
    return parser.parse_args()


def iter_search_windows(args: argparse.Namespace) -> Iterable[Tuple[str, Dict]]:
    current_year = dt.datetime.now().year
    if args.mode == "daily":
        yield f"last_{args.daily_days}_days", {"reldate": args.daily_days, "datetype": args.datetype}
        return
    # Optional upper bound so a single year (or a bounded window) can be fetched;
    # defaults to the current year to preserve the original start_year..present behavior.
    end_year = getattr(args, "end_year", 0) or current_year
    for year in range(args.start_year, end_year + 1):
        yield str(year), {
            "mindate": f"{year}/01/01",
            "maxdate": f"{year}/12/31",
            "datetype": args.datetype,
        }


def build_rows(
    *,
    record: PubMedRecord,
    molecule: dict,
    molecule_id: str,
    molecule_name: str,
    rule_id: str,
    match_strength: str,
    query: str,
    run_id: str,
    summary_agent,
    role_rules: list,
) -> Tuple[dict, dict]:
    now = utc_now_iso()
    classification = classify_record(record)
    relevance = classify_molecule_relevance(record, molecule)
    summary = summary_agent.summarize(record, molecule_name=molecule_name)

    paper = record.to_dict()
    paper["updated_at_utc"] = now

    evidence = {
        "evidence_id": f"{record.pmid}:{molecule_id}:{rule_id}",
        "pmid": record.pmid,
        "molecule_id": molecule_id,
        "molecule_name": molecule_name,
        "rule_id": rule_id,
        "match_strength": match_strength,
        "source_query_hash": hashlib.sha1(query.encode("utf-8")).hexdigest()[:12],
        "run_id": run_id,
        "fetched_at_utc": now,
        "pub_year": record.pub_year,
        "review_status": "needs_review",
        **_prefixed_classification(classification.to_dict()),
        **relevance.to_dict(),
        **summary.to_dict(),
    }
    if role_rules:
        role = classify_role(evidence, paper, molecule, role_rules)
        evidence.update(role.to_dict())
        evidence["website_include"] = role.public_candidate
        evidence["review_status"] = _review_status_from_role_bucket(role.role_review_bucket)
    paper_characterization = characterize_paper(evidence, paper, molecule)
    evidence.update(paper_characterization.to_dict())
    evidence.update(route_evidence(evidence).to_dict())
    return paper, evidence


def build_profiles_for_output(args: argparse.Namespace, molecules: dict, new_evidence: List[dict], updated_at: str) -> List[dict]:
    if "local" in {x.strip().lower() for x in args.sinks.split(",")} and os.path.exists(args.local_db):
        try:
            evidence_rows = load_evidence_payloads(args.local_db)
            missing_relevance = sum(1 for row in evidence_rows if "molecule_relevance" not in row)
            if missing_relevance:
                print(
                    f"Profile warning: {missing_relevance} local evidence rows predate relevance fields. "
                    "Run with --refresh-seen to regenerate cleaner profiles.",
                    flush=True,
                )
        except Exception as exc:
            print(f"Could not load local evidence for profiles; using current run only: {exc}", flush=True)
            evidence_rows = new_evidence
    else:
        evidence_rows = new_evidence
    return build_molecule_profiles(molecules, evidence_rows, updated_at=updated_at)


def _prefixed_classification(data: dict) -> dict:
    return {
        "primary_study_type": data.get("primary_study_type", ""),
        "study_design_tags": data.get("study_design_tags", ""),
        "model_type": data.get("model_type", ""),
        "species_or_population": data.get("species_or_population", ""),
        "human_flag": data.get("human_flag", False),
        "animal_flag": data.get("animal_flag", False),
        "in_vitro_flag": data.get("in_vitro_flag", False),
        "classification_confidence": data.get("confidence", ""),
        "classification_notes": data.get("notes", ""),
    }


def _review_status_from_role_bucket(bucket: str) -> str:
    mapping = {
        "public_candidate": "machine_public_candidate",
        "curator_review": "needs_review",
        "background_only": "machine_background_only",
        "exclude_noise": "machine_exclude",
    }
    return mapping.get(bucket, "needs_review")


def google_client():
    import gspread
    from google.auth import default

    creds, _ = default(scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ])
    return gspread.authorize(creds)


if __name__ == "__main__":
    main()
