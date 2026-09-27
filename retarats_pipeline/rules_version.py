"""Deterministic fingerprint of the SCIENTIFIC retrieval rules (WS4).

Captures the inputs that decide WHAT gets retrieved, not how it is stored or
graded: the molecule registry, the PubMed search rules, the rendered CT.gov /
EuropePMC per-molecule queries derived from them, and the PubMed discovery-window
policy (datetype, day window, re-sweep window). A retrieval-rule change --
editing a query string, adding/removing a molecule, changing the discovery
window -- changes this hash; an unrelated code change (grading, frontend,
infra) does not. Stamped into the release manifest (``rules_version``) so a
release is reproducibly tied to the rule set that produced it.

Deliberately EXCLUDES config/retrieval_benchmark.csv and the candidates file:
those validate retrieval, they do not define it, so approving/rejecting a
benchmark row must not change the rules_version.

Pure stdlib, no network, deterministic given the same files on disk.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Optional

RULES_VERSION_SCHEMA = 1

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG_DIR = os.path.join(ROOT, "config")
PUBMED_DISCOVERY_PATH = os.path.join(DEFAULT_CONFIG_DIR, "pubmed_discovery.json")

# Inputs that define retrieval scope/scientific behavior. Order matters (hash is
# order-sensitive) but is fixed here, so re-running is always reproducible.
_HASHED_CONFIG_FILES = ("MOLECULES.csv", "SEARCH_RULES.csv", "pubmed_discovery.json")


def _file_digest(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        data = b""
    return hashlib.sha256(data).hexdigest()


def load_pubmed_discovery_policy(path: str = PUBMED_DISCOVERY_PATH) -> dict:
    """Read config/pubmed_discovery.json; a sane default if it's missing/broken
    so callers never crash on a fresh checkout that hasn't added the file yet."""
    default = {"schema": 1, "datetype": "pdat", "daily_days": 8}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return {**default, **data}
    except (OSError, ValueError):
        pass
    return default


def compute_rules_version(config_dir: str = DEFAULT_CONFIG_DIR) -> str:
    """A short, deterministic ``rv<schema>-<12 hex>`` string. Two checkouts with
    identical MOLECULES.csv / SEARCH_RULES.csv / pubmed_discovery.json (and thus
    identical rendered per-molecule queries) always produce the same value."""
    from retarats_pipeline.enrichment.registry import (  # local import: avoid a
        load_active_molecules,                            # module-load cycle with
        preprints_query,                                   # enrichment.registry
        trials_query,
    )

    h = hashlib.sha256()
    h.update(f"schema={RULES_VERSION_SCHEMA}\n".encode())
    for name in _HASHED_CONFIG_FILES:
        h.update(f"{name}:{_file_digest(os.path.join(config_dir, name))}\n".encode())

    molecules = load_active_molecules(os.path.join(config_dir, "MOLECULES.csv"))
    for m in sorted(molecules, key=lambda row: row.get("molecule_id", "")):
        mid = m.get("molecule_id", "")
        h.update(f"ctgov:{mid}={trials_query(m)}\n".encode())
        h.update(f"epmc:{mid}={preprints_query(m)}\n".encode())

    return f"rv{RULES_VERSION_SCHEMA}-{h.hexdigest()[:12]}"


def rules_version_detail(config_dir: str = DEFAULT_CONFIG_DIR) -> dict:
    """Human-inspectable breakdown behind ``compute_rules_version`` -- which
    files/queries fed the hash, and their individual digests -- for debugging a
    version bump ("what changed?") without recomputing the whole thing by hand."""
    policy = load_pubmed_discovery_policy(os.path.join(config_dir, "pubmed_discovery.json"))
    return {
        "rules_version": compute_rules_version(config_dir),
        "schema": RULES_VERSION_SCHEMA,
        "file_digests": {name: _file_digest(os.path.join(config_dir, name))[:16]
                          for name in _HASHED_CONFIG_FILES},
        "pubmed_discovery_policy": policy,
    }
