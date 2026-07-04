from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path


def test_offline_csv_only(db_path: str, package_root: str, tmp_dir: str) -> None:
    out = Path(tmp_dir) / "enriched"
    queue = Path(tmp_dir) / "review_queue"
    cmd = [
        sys.executable,
        str(Path(package_root) / "scripts" / "run_enrichment_pipeline.py"),
        "--db", db_path,
        "--out-dir", str(out),
        "--review-queue-dir", str(queue),
        "--offline",
        "--csv-only",
        "--max-records", "25",
    ]
    subprocess.check_call(cmd)
    expected = [
        out / "human_intervention_enrichment_audit.csv",
        out / "basic_science_enrichment_audit.csv",
        out / "pmc_full_text_audit.csv",
        out / "evidence_enriched_subset.csv",
        queue / "pico_incomplete.csv",
        queue / "basic_science_incomplete.csv",
    ]
    missing = [str(p) for p in expected if not p.exists()]
    if missing:
        raise AssertionError(f"missing expected outputs: {missing}")


def test_sqlite_write(db_path: str, package_root: str, tmp_dir: str) -> None:
    Path(tmp_dir).mkdir(parents=True, exist_ok=True)
    db_copy = Path(tmp_dir) / "test.sqlite"
    shutil.copy2(db_path, db_copy)
    cmd = [
        sys.executable,
        str(Path(package_root) / "scripts" / "run_enrichment_pipeline.py"),
        "--db", str(db_copy),
        "--out-dir", str(Path(tmp_dir) / "write_enriched"),
        "--review-queue-dir", str(Path(tmp_dir) / "write_queue"),
        "--offline",
        "--max-records", "10",
    ]
    subprocess.check_call(cmd)
    conn = sqlite3.connect(db_copy)
    count = 0
    for (payload_json,) in conn.execute("select payload_json from evidence limit 500"):
        payload = json.loads(payload_json)
        if any(k.startswith("enriched_") for k in payload):
            count += 1
    conn.close()
    if count == 0:
        raise AssertionError("no enriched_* fields were written to copied SQLite database")


if __name__ == "__main__":
    root = str(Path(__file__).resolve().parents[1])
    db = sys.argv[1] if len(sys.argv) > 1 else "data/retarats_pubmed.sqlite"
    tmp = sys.argv[2] if len(sys.argv) > 2 else "/tmp/retarats_enrichment_smoke"
    Path(tmp).mkdir(parents=True, exist_ok=True)
    test_offline_csv_only(db, root, str(Path(tmp) / "csv_only"))
    test_sqlite_write(db, root, str(Path(tmp) / "sqlite_write"))
    print("smoke tests passed")
