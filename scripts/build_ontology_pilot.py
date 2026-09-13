#!/usr/bin/env python3
"""Reproducible, stratified 48-report ontology pilot from a read-only corpus.

Outputs complete sources, machine annotations, and an empty review form. Selection
is deterministic and capped per molecule within each stratum. This is a development
sample, not an accuracy benchmark. No inferred findings or human reviews are created.
"""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from retarats_pipeline.curation.ontology import annotate, VERSION

STRATA = ['RCT', 'Human observational', 'Animal in vivo', 'In vitro / cell',
          'Systematic review / Meta-analysis', 'Review / narrative',
          'Methods / Mechanistic', 'Case report / Case series']

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--db', default='data/retarats_pubmed.sqlite')
    ap.add_argument('--out-dir', default='exports/ontology_pilot')
    args = ap.parse_args()
    out = Path(args.out_dir); out.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(Path(args.db).resolve().as_uri() + '?mode=ro', uri=True)
    papers = {str(k): json.loads(v) for k, v in conn.execute('select pmid,payload_json from papers')}
    evidence = [json.loads(v) for v, in conn.execute('select payload_json from evidence')]
    conn.close()
    selected = []; seen = set()
    for stratum in STRATA:
        candidates = [e for e in evidence if e.get('primary_study_type') == stratum and
                      papers.get(str(e.get('pmid')), {}).get('abstract')]
        candidates.sort(key=lambda e: hashlib.sha256(str(e['evidence_id']).encode()).hexdigest())
        molecules = {}; count = 0
        for e in candidates:
            pmid = str(e.get('pmid')); mol = e['molecule_id']
            if pmid in seen or molecules.get(mol, 0) >= 2:
                continue
            p = papers[pmid]; o, annotations = annotate(e, p)
            selected.append(dict(stratum=stratum, evidence=e, paper=p, ontology=o, annotations=annotations))
            seen.add(pmid); molecules[mol] = molecules.get(mol, 0) + 1; count += 1
            if count == 6:
                break
    with (out/'sources.jsonl').open('w') as f:
        for r in selected:
            f.write(json.dumps(r, ensure_ascii=False)+'\n')
    fields = ['evidence_id','pmid','molecule_id','stratum','title','evidence_scope',
              'condition_studied','outcome_measured','review_status','reviewer','notes']
    # Never overwrite an existing review; a fresh selection goes to a new file.
    review = out/'review.csv'
    if review.exists():
        review = out/'review_proposed.csv'
    with review.open('w', newline='') as f:
        w=csv.DictWriter(f, fieldnames=fields); w.writeheader()
        for r in selected:
            e,p,o=r['evidence'],r['paper'],r['ontology']
            w.writerow(dict(evidence_id=e['evidence_id'],pmid=e['pmid'],molecule_id=e['molecule_id'],
                stratum=r['stratum'],title=p['title'],evidence_scope=o['evidence_scope'],
                condition_studied=o['facet_condition_studied'],outcome_measured=o['facet_outcome_measured'],
                review_status='machine_unreviewed',reviewer='',notes=''))
    db=out/'pilot.sqlite'
    # This DB is a reproducible derivative, not the source corpus.
    if db.exists(): db.unlink()
    c=sqlite3.connect(db)
    c.execute('create table papers (pmid text primary key,payload_json text)')
    c.execute('create table evidence (evidence_id text primary key,payload_json text)')
    c.executemany('insert into papers values (?,?)', [(str(r['evidence']['pmid']),json.dumps(r['paper'])) for r in selected])
    c.executemany('insert into evidence values (?,?)', [(str(r['evidence']['evidence_id']),json.dumps(r['evidence'])) for r in selected])
    c.commit();c.close()
    print(f'{len(selected)} distinct reports; ontology {VERSION}; {out}')

if __name__ == '__main__': main()
