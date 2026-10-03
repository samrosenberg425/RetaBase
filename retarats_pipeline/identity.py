"""Molecule identity: is a stored SOURCE RECORD really about the MOLECULE it is filed under?

Retrieval (what a search returns) and identity (what a record is about) are separate questions.
Searches match on tokenisation, server-side synonym expansion and full text, so a hit is not proof
of identity; and a partial or failed retrieval says nothing about records we already hold. This
module answers the identity question ONLY, from the stored text, offline and deterministically:

    evaluate(config, source, molecule_id, zones, key) -> Verdict(outcome, match_type, term, ...)

Outcomes (a record is published for its molecule only on ``pass``; nothing is ever deleted):

* ``pass``    -- the record names the molecule (or a human said to keep it).
* ``hold``    -- no identity evidence in the available text, or only an ambiguous alias without its
                 required context. Reversible: change the rules and re-run.
* ``exclude`` -- positive evidence of a known UNRELATED meaning (an exclusion term next to a
                 non-canonical match). Also reversible; reported separately from ``hold``.

Term roles (config/MOLECULE_IDENTITY.csv is an OVERLAY on config/MOLECULES.csv, which stays the
backwards-compatible default: display name + every synonym are identity names unless the overlay
says otherwise):

* ``canonical``         display name (implicit).
* ``specific_alias``    a name specific enough to establish identity alone (e.g. thymalfasin).
* ``contextual_alias``  an ambiguous token (VIP, LDN, TB4, MT-II ...): counts only when ``context_any``
                        text is also present (and no ``exclude_any`` text). Acronym-shaped terms are
                        matched case-sensitively.
* ``exclusion``         terms marking a known unrelated meaning; they veto every NON-canonical match.
* manual keep           config/registry_keep.csv, approved benchmark includes, manual_pmids.csv,
                        gold-standard PMIDs -- override every automated outcome.

Separate from the role is DISCOVERY: ``discovery`` lists the sources (ctgov, preprints) whose search
gets the term in addition to the default query. PubMed discovery is governed by SEARCH_RULES.csv only;
this overlay never changes PubMed retrieval.

Stdlib only, no I/O beyond reading config and (for ``reevaluate_*``) the SQLite payload tables.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

ENGINE_VERSION = "1"
IDENTITY_PATH = os.path.join("config", "MOLECULE_IDENTITY.csv")
MOLECULES_PATH = os.path.join("config", "MOLECULES.csv")
REGISTRY_KEEP_PATH = os.path.join("config", "registry_keep.csv")
BENCHMARK_PATH = os.path.join("config", "retrieval_benchmark.csv")
MANUAL_PMIDS_PATH = os.path.join("config", "manual_pmids.csv")
GOLD_PATH = os.path.join("config", "gold_standard_pmids.csv")
POLICY_PATH = os.path.join("config", "identity_policy.csv")

SOURCES = ("pubmed", "ctgov", "preprints")
DISCOVERY_SOURCES = ("ctgov", "preprints")  # PubMed discovery lives in SEARCH_RULES.csv, not here
ROLES = ("canonical", "specific_alias", "contextual_alias", "exclusion")

# Stored text a source can be judged on, and the ROLE each zone plays in the association. Registry rows keep
# only part of the study record, so a trial is judged on what we hold. Roles (provenance, and policy):
#   exposure          intervention names, other names, arm groups   (the molecule is given / used)
#   subject           titles, conditions, keywords                   (the study is about it)
#   measured_outcome  outcome measure titles + outcome descriptions   (a biomarker / measured readout)
#   background        brief summary / detailed description, eligibility (mentioned, not the subject)
#   text_mention      preprint / PubMed abstract
#   indexing          MeSH headings, substance names (exact heading only; never a sibling/derivative term)
SOURCE_ZONES: Dict[str, Tuple[str, ...]] = {
    "pubmed": ("title", "abstract", "keywords", "mesh_terms", "chemicals"),
    "ctgov": ("brief_title", "official_title", "conditions", "keywords", "interventions", "other_names", "arms",
              "outcome_measures", "outcome_text", "summary_text", "eligibility_text"),
    "preprints": ("title", "abstract"),
}
ZONE_ROLE: Dict[str, Dict[str, str]] = {
    "pubmed": {"title": "subject", "keywords": "subject", "abstract": "text_mention",
               "mesh_terms": "indexing", "chemicals": "indexing"},
    "ctgov": {"interventions": "exposure", "other_names": "exposure", "arms": "exposure",
              "brief_title": "subject", "official_title": "subject", "conditions": "subject", "keywords": "subject",
              "outcome_measures": "measured_outcome",
              "outcome_text": "measured_outcome", "summary_text": "background", "eligibility_text": "background"},
    "preprints": {"title": "subject", "abstract": "text_mention"},
}
ROLE_ORDER = ("exposure", "subject", "measured_outcome", "text_mention", "indexing", "background")
# Roles that may ESTABLISH identity. config/identity_policy.csv (publish_roles) overrides per source.
DEFAULT_ESTABLISHING: Dict[str, frozenset] = {
    "ctgov": frozenset({"exposure", "subject", "measured_outcome"}),
    # MeSH / substance headings never establish identity on their own: papers about derivatives and neighbours
    # (zotarolimus / everolimus stents, isoquercitrin, taurolidine, acamprosate ...) carry the parent descriptor.
    "pubmed": frozenset(ROLE_ORDER) - {"indexing"},
    "preprints": frozenset(ROLE_ORDER),
}
# Zones that are indexing metadata, not prose: they can prove identity for canonical/specific names
# only, and only when a whole heading equals the name (a MeSH heading is not a context sentence, and a
# neighbouring concept -- taurocholic acid, spermine, everolimus -- is not the molecule).
INDEXING_ZONES = {"mesh_terms", "chemicals"}
# A source whose records normally carry an abstract is fail-open when the abstract is missing
# AND the title does not name the molecule (nothing to judge from).
ABSTRACT_ZONE = {"pubmed": "abstract", "preprints": "abstract"}
# ... and, for PubMed, only when no indexing/keyword text exists either (a record with MeSH headings that do not
# name the molecule HAS something to judge).
SUPPORT_ZONES = {"pubmed": ("keywords", "mesh_terms", "chemicals"), "preprints": ()}

PASS, HOLD, EXCLUDE = "pass", "hold", "exclude"
M_CANONICAL, M_SPECIFIC, M_CONTEXTUAL = "canonical_name", "specific_alias", "contextual_alias"
M_KEEP, M_NONE, M_EXCLUSION, M_INSUFFICIENT, M_NO_RULES = (
    "manual_keep", "none", "exclusion_term", "insufficient_text", "no_identity_rules")
M_BACKGROUND, M_LEGACY, M_INDEXING = "background_mention", "legacy_unverified", "indexing_only"
# Provenance classes of the association (Verdict.role): the zone roles above plus
R_AMBIGUOUS, R_UNRELATED, R_UNVERIFIED, R_KEEP, R_INSUFFICIENT = (
    "ambiguous_acronym", "unrelated", "unverified", "manual_keep", "insufficient")

_AMBIG_NOTE = re.compile(r"\(?\b(alone|broad|ambiguous|only)\b\)?", re.IGNORECASE)


# ---------------------------------------------------------------------------------------------
# term matching
# ---------------------------------------------------------------------------------------------

def _alnum_tokens(term: str) -> List[str]:
    """Letter runs and digit runs ('TB4' -> TB, 4; '17α-estradiol' -> 17, α, estradiol), so spelling variants
    that only differ in a separator ('TB4' / 'TB-4' / 'TB 4', '17α' / '17-α' / '17 α') match alike."""
    return re.findall(r"[^\W\d_]+|\d+", term, flags=re.UNICODE)


def is_acronym(term: str) -> bool:
    """All-uppercase letters/digits/punctuation, short: VIP, LDN, TB-4, MT-II, TA1."""
    letters = [c for c in term if c.isalpha()]
    return bool(letters) and all(c.isupper() for c in letters) and len("".join(_alnum_tokens(term))) <= 6


def compile_term(term: str, case_sensitive: bool = False) -> "re.Pattern[str]":
    """Whole-token match. Between the term's letter/digit pieces any space/underscore/hyphen/dash/bracket is
    optional and interchangeable, plus whatever punctuation the term itself spells there, so
    'MT-II' finds 'MT II' and 'MTII', 'LL-37' finds 'LL37', and 'PTH(1-34)' finds 'PTH(1-34)' and
    'PTH 1-34'. Other punctuation is NOT skipped ('TB, 4' is not 'TB-4'), and the match never starts
    or ends inside a longer word ('metallothionein' does not match inside 'metallothioneins')."""
    toks = _alnum_tokens(term)
    if not toks:
        return re.compile(r"(?!x)x")
    seps = set("".join(re.findall(r"[^\w\s]|_", term.strip())))
    # hyphen-like characters, incl. the Unicode variants PubMed abstracts use ('angiotensin-(1⁻7)', minus sign)
    dash = r"\-\u2010-\u2015\u207b\u208b\u2212\ufe63\uff0d"
    extra = "".join(re.escape(c) for c in sorted(seps) if c not in "-_()[]{}:")
    # always optional between pieces: space/underscore, dashes, brackets, colon ('PTH [1-34]', 'beta(4)',
    # 'acetyl-L: -carnitine'); plus any other punctuation the term itself spells ('ActRIIB.Fc').
    joiner = r"\s_" + dash + r"()\[\]{}:/" + extra
    # two adjacent digit runs need a REAL separator (space / dash / the term's own punctuation), never a bracket:
    # '22-2' must not match '222' or a cytogenetic '(p22)[2]'
    digit_joiner = r"\s_" + dash + extra
    parts = [re.escape(toks[0])]
    for prev, tok in zip(toks, toks[1:]):
        parts.append((f"[{digit_joiner}]+" if prev.isdigit() and tok.isdigit() else f"[{joiner}]*") + re.escape(tok))
    body = "".join(parts)
    # a plain-word name also matches its plural ('Kisspeptins'); acronyms and codes do not
    plural = "s?" if (not case_sensitive and toks[-1].isalpha() and len(toks[-1]) >= 5 and not is_acronym(term)) else ""
    flags = 0 if case_sensitive else re.IGNORECASE
    return re.compile(r"(?<![^\W_])" + body + plural + r"(?![^\W_])", flags | re.UNICODE)


def compile_context(term: str) -> "re.Pattern[str]":
    """Context / veto terms: acronyms (MSH, MC4R, PACAP) are whole-token and case-sensitive; ordinary
    words only need a word START, so 'thymopoietin' also finds 'thymopoietin32-36', 'interneuron'
    finds 'interneurons' and 'metallothionein' finds 'metallothioneins'."""
    if is_acronym(term):
        return compile_term(term, True)
    base = compile_term(term, False)
    return re.compile(base.pattern.rsplit("(?![^\\W_])", 1)[0], base.flags)


def _split_terms(value: str) -> List[str]:
    return [t.strip() for t in (value or "").split("|") if t.strip()]


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"true", "1", "yes", "y", "t"}


# ---------------------------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Rule:
    molecule_id: str
    term: str
    role: str
    pattern: "re.Pattern[str]"
    context: Tuple["re.Pattern[str]", ...] = ()
    context_terms: Tuple[str, ...] = ()
    veto: Tuple["re.Pattern[str]", ...] = ()
    veto_terms: Tuple[str, ...] = ()
    applies_to: frozenset = frozenset(SOURCES)
    discovery: frozenset = frozenset()
    zones: frozenset = frozenset()      # restrict where the term may match (empty = every zone of the source)
    case_sensitive: bool = False
    origin: str = "overlay"      # overlay | default
    evidence: str = ""


@dataclass
class MoleculeIdentity:
    molecule_id: str
    display_name: str
    canonical: List[Rule] = field(default_factory=list)
    specific: List[Rule] = field(default_factory=list)
    contextual: List[Rule] = field(default_factory=list)
    exclusions: List[Rule] = field(default_factory=list)


@dataclass
class IdentityConfig:
    by_molecule: Dict[str, MoleculeIdentity] = field(default_factory=dict)
    keep: Set[Tuple[str, str]] = field(default_factory=set)       # (molecule_id, KEY) -- KEY upper-cased
    version: str = ""
    warnings: List[str] = field(default_factory=list)
    roles_ok: Dict[str, frozenset] = field(default_factory=lambda: dict(DEFAULT_ESTABLISHING))

    def discovery_terms(self, molecule_id: str, source: str) -> List[str]:
        mi = self.by_molecule.get(molecule_id)
        if not mi:
            return []
        out, seen = [], set()
        for rule in mi.canonical + mi.specific + mi.contextual:
            if source in rule.discovery and rule.term.lower() not in seen:
                seen.add(rule.term.lower())
                out.append(rule.term)
        return out


def _read_csv(path: str) -> List[dict]:
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def ambiguous_tokens(molecule: Mapping[str, str]) -> Set[str]:
    """Terms the MOLECULES.csv ``exclusions_csv`` column already says must never be used alone
    ('NR (alone)', 'VIP alone broad', 'MSTN' ...). Lower-cased, qualifiers stripped."""
    out = set()
    for raw in str(molecule.get("exclusions_csv", "") or "").split(","):
        t = _AMBIG_NOTE.sub("", raw).strip(" ()").strip()
        if t:
            out.add(t.lower())
    return out


def _display_variants(display_name: str) -> List[str]:
    """'VIP (Vasoactive Intestinal Peptide)' -> ['VIP (Vasoactive Intestinal Peptide)', 'VIP',
    'Vasoactive Intestinal Peptide']; plain names pass through."""
    name = (display_name or "").strip()
    out = [name] if name else []
    m = re.match(r"^(.*?)\s*\((.*)\)\s*$", name)
    if m:
        for part in (m.group(1), m.group(2)):
            part = part.strip()
            if part and part.lower() not in {o.lower() for o in out}:
                out.append(part)
    return out


def _mk_rule(molecule_id: str, term: str, role: str, **kw) -> Rule:
    cs = kw.pop("case_sensitive", None)
    if cs is None:
        cs = role == "contextual_alias" and is_acronym(term)
    ctx = tuple(kw.pop("context_terms", ()))
    veto = tuple(kw.pop("veto_terms", ()))
    return Rule(
        molecule_id=molecule_id, term=term, role=role, pattern=compile_term(term, cs),
        context=tuple(compile_context(c) for c in ctx), context_terms=ctx,
        veto=tuple(compile_context(v) for v in veto), veto_terms=veto,
        case_sensitive=bool(cs), **kw)


def load_keep(registry_keep: str = REGISTRY_KEEP_PATH, benchmark: str = BENCHMARK_PATH,
              manual_pmids: str = MANUAL_PMIDS_PATH, gold: str = GOLD_PATH) -> Set[Tuple[str, str]]:
    """(molecule_id, KEY) a human has said belong: registry_keep, approved benchmark includes,
    manual_pmids force-includes, gold-standard must-retrieve PMIDs. KEY is upper-cased."""
    out: Set[Tuple[str, str]] = set()
    for r in _read_csv(registry_keep):
        out.add(((r.get("molecule_id") or "").strip(), (r.get("id") or "").strip().upper()))
    for r in _read_csv(benchmark):
        if (r.get("status", "").strip().lower() == "approved" and r.get("expected", "").strip().lower() == "include"):
            out.add((r.get("molecule_id", "").strip(), r.get("id", "").strip().upper()))
    for r in _read_csv(manual_pmids):
        out.add(((r.get("molecule_id") or "").strip(), (r.get("pmid") or "").strip().upper()))
    for r in _read_csv(gold):
        for pmid in re.split(r"[;\s]+", r.get("must_retrieve_pmids", "") or ""):
            if pmid.isdigit():
                out.add((r.get("molecule_id", "").strip(), pmid))
    out.discard(("", ""))
    return {k for k in out if k[0] and k[1]}


def load_establishing_roles(warnings: Optional[List[str]] = None, path: str = None) -> Dict[str, frozenset]:
    """Per source, the match ROLES that may establish identity (config/identity_policy.csv, column
    publish_roles, ';'-separated). Absent file / blank cell = DEFAULT_ESTABLISHING."""
    out = dict(DEFAULT_ESTABLISHING)
    for r in _read_csv(path or POLICY_PATH):
        src = (r.get("source") or "").strip().lower()
        raw = (r.get("publish_roles") or "").strip()
        if src not in SOURCES or not raw:
            continue
        roles = frozenset(x.strip() for x in raw.replace("|", ";").split(";") if x.strip())
        bad = roles - set(ROLE_ORDER)
        if bad or not roles:
            if warnings is not None:
                warnings.append(f"{POLICY_PATH}: {src}: unknown publish_roles {sorted(bad)}; default kept")
            continue
        out[src] = roles
    return out


def load_identity_config(molecules: Optional[Sequence[Mapping[str, str]]] = None,
                         overlay_path: str = IDENTITY_PATH, keep: Optional[Set[Tuple[str, str]]] = None,
                         molecules_path: str = MOLECULES_PATH) -> IdentityConfig:
    """Build the identity rules: MOLECULES.csv defaults + the overlay. Never raises on a bad overlay
    row -- the row is skipped with a warning and the molecule keeps its default (backwards-compatible)
    behaviour."""
    if molecules is None:
        from retarats_pipeline.enrichment.registry import load_active_molecules
        molecules = load_active_molecules(molecules_path)
    cfg = IdentityConfig(keep=set(keep) if keep is not None else load_keep())
    cfg.roles_ok = load_establishing_roles(cfg.warnings)
    overlay_rows = _read_csv(overlay_path)
    by_mol_overlay: Dict[str, List[Tuple[int, dict]]] = {}
    for i, r in enumerate(overlay_rows, start=2):
        mid = (r.get("molecule_id") or "").strip()
        if mid:
            by_mol_overlay.setdefault(mid, []).append((i, r))

    known = {str(m.get("molecule_id", "")).strip() for m in molecules}
    for mid, rows in by_mol_overlay.items():
        if mid not in known:
            cfg.warnings.append(f"{overlay_path}: unknown molecule_id {mid!r}; {len(rows)} row(s) ignored")

    for m in molecules:
        mid = str(m.get("molecule_id", "")).strip()
        if not mid:
            continue
        display = str(m.get("display_name", "") or "").strip()
        mi = MoleculeIdentity(mid, display)
        overlay = by_mol_overlay.get(mid, [])
        overlay_terms = {t.lower() for _, r in overlay for t in _split_terms(r.get("term", ""))}
        ambiguous = ambiguous_tokens(m)

        # defaults from MOLECULES.csv (the old name-guard behaviour), unless the overlay classifies the term
        seen: Set[str] = set()
        for v in _display_variants(display):
            if v.lower() in overlay_terms or v.lower() in seen or len("".join(_alnum_tokens(v))) < 3:
                continue
            seen.add(v.lower())
            role = "contextual_alias" if v.lower() in ambiguous else "canonical"
            mi_list = mi.contextual if role == "contextual_alias" else mi.canonical
            mi_list.append(_mk_rule(mid, v, role, origin="default"))
        for syn in str(m.get("synonyms_csv", "") or "").split(","):
            syn = syn.strip()
            if (not syn or syn.lower() in overlay_terms or syn.lower() in seen
                    or len("".join(_alnum_tokens(syn))) < 3):
                continue
            seen.add(syn.lower())
            if syn.lower() in ambiguous:
                mi.contextual.append(_mk_rule(mid, syn, "contextual_alias", origin="default"))
            else:
                mi.specific.append(_mk_rule(mid, syn, "specific_alias", origin="default"))

        for lineno, r in overlay:
            where = f"{overlay_path}:{lineno}"
            role = (r.get("role") or "").strip().lower()
            terms = _split_terms(r.get("term", ""))
            if role not in ROLES or not terms:
                cfg.warnings.append(f"{where}: bad role/term ({role!r}); row ignored")
                continue
            ctx = _split_terms(r.get("context_any", ""))
            veto = _split_terms(r.get("exclude_any", ""))
            applies = frozenset(s for s in _split_terms((r.get("applies_to") or "").replace(";", "|")) if s in SOURCES) or frozenset(SOURCES)
            disc = frozenset(s for s in _split_terms((r.get("discovery") or "").replace(";", "|")) if s in DISCOVERY_SOURCES)
            zset = frozenset(_split_terms((r.get("zones") or "").replace(";", "|")))
            raw_disc = set(_split_terms((r.get("discovery") or "").replace(";", "|")))
            if raw_disc - set(DISCOVERY_SOURCES):
                cfg.warnings.append(f"{where}: discovery {sorted(raw_disc - set(DISCOVERY_SOURCES))} ignored "
                                    f"(only {DISCOVERY_SOURCES}; PubMed discovery is SEARCH_RULES.csv)")
            if role == "contextual_alias" and not ctx and not veto:
                cfg.warnings.append(f"{where}: contextual_alias {terms} has no context_any/exclude_any; "
                                    f"it can never establish identity alone")
            cs_raw = (r.get("case_sensitive") or "").strip()
            for term in terms:
                rule = _mk_rule(mid, term, role, context_terms=ctx, veto_terms=veto, applies_to=applies,
                                discovery=disc, zones=zset, case_sensitive=(_truthy(cs_raw) if cs_raw else None),
                                evidence=(r.get("evidence") or "").strip())
                {"canonical": mi.canonical, "specific_alias": mi.specific, "contextual_alias": mi.contextual,
                 "exclusion": mi.exclusions}[role].append(rule)
        cfg.by_molecule[mid] = mi

    cfg.version = rules_version(cfg, molecules, overlay_path)
    return cfg


_CONFIG_CACHE: Dict[tuple, IdentityConfig] = {}


def get_config(overlay_path: str = IDENTITY_PATH, molecules_path: str = MOLECULES_PATH) -> IdentityConfig:
    """Cached ``load_identity_config`` for the default files; the cache key includes every input file's
    path/mtime/size, so editing config (or a test chdir-ing into a temp config dir) is picked up."""
    sig = []
    for p in (overlay_path, molecules_path, REGISTRY_KEEP_PATH, BENCHMARK_PATH, MANUAL_PMIDS_PATH, GOLD_PATH,
              POLICY_PATH):
        ap = os.path.abspath(p)
        try:
            st = os.stat(ap)
            sig.append((ap, st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append((ap, 0, 0))
    key = tuple(sig)
    cfg = _CONFIG_CACHE.get(key)
    if cfg is None:
        _CONFIG_CACHE.clear()
        cfg = load_identity_config(None, overlay_path, None, molecules_path)
        _CONFIG_CACHE[key] = cfg
    return cfg


def rules_version(cfg: IdentityConfig, molecules: Sequence[Mapping[str, str]], overlay_path: str = IDENTITY_PATH) -> str:
    """Deterministic fingerprint of everything that can change a verdict (engine, resolved rules, keep set)."""
    h = hashlib.sha256()
    h.update(f"engine={ENGINE_VERSION}\n".encode())
    for mid in sorted(cfg.by_molecule):
        mi = cfg.by_molecule[mid]
        for bucket, rules in (("C", mi.canonical), ("S", mi.specific), ("X", mi.contextual), ("E", mi.exclusions)):
            for r in sorted(rules, key=lambda r: (r.term.lower(), r.role)):
                h.update(json.dumps([mid, bucket, r.term, r.pattern.pattern, r.case_sensitive, r.context_terms,
                                     r.veto_terms, sorted(r.applies_to), sorted(r.zones)], ensure_ascii=False).encode())
                h.update(b"\n")
    for k in sorted(cfg.keep):
        h.update(("keep|" + "|".join(k) + "\n").encode())
    for src in sorted(cfg.roles_ok):
        h.update(("roles|" + src + "|" + ",".join(sorted(cfg.roles_ok[src])) + "\n").encode())
    return "id" + ENGINE_VERSION + "-" + h.hexdigest()[:12]


# ---------------------------------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Verdict:
    outcome: str
    match_type: str
    matched_term: str = ""
    zone: str = ""
    reason: str = ""
    role: str = ""      # exposure | subject | measured_outcome | background | text_mention | indexing | ambiguous_acronym | unrelated | ...

    @property
    def published(self) -> bool:
        return self.outcome == PASS


def indexing_headings(text: str) -> List[str]:
    """The headings of a MeSH / substance list as stored (``"; "``-joined elements like
    ``'Sirolimus: pharmacology, therapeutic use'``): the part before the qualifier colon."""
    return [e.split(":", 1)[0].strip() for e in str(text or "").split("; ") if e.strip()]


def mesh_exact_hit(rule: Rule, text: str) -> str:
    """The heading in ``text`` that EQUALS the rule's name (whole heading, same separator/plural tolerance as
    ordinary matching), else "". A heading that merely CONTAINS the name ('Quercetin-3-glucoside', 'Metformin
    adduct') or is a neighbouring concept (Taurocholic Acid, Spermine, Everolimus) is not a hit."""
    for h in indexing_headings(text):
        if rule.pattern.fullmatch(h):
            return h
    return ""


def _first_zone_hit(rule: Rule, zones: Mapping[str, str], allowed: Iterable[str]) -> str:
    for z in allowed:
        if rule.zones and z not in rule.zones:
            continue
        txt = zones.get(z, "")
        if not txt:
            continue
        if z in INDEXING_ZONES:
            if mesh_exact_hit(rule, txt):
                return z
        elif rule.pattern.search(txt):
            return z
    return ""


def evaluate(cfg: IdentityConfig, source: str, molecule_id: str, zones: Mapping[str, object],
             key: str = "") -> Verdict:
    """Judge ONE record against the molecule it is filed under. Pure and deterministic.

    ``zones`` maps zone name -> stored text (see SOURCE_ZONES). The verdict carries a ROLE (how the molecule
    appears: exposure / subject / measured_outcome / background / text_mention / indexing, or ambiguous_acronym /
    unrelated). Only roles listed for the source (identity_policy.csv; trials: exposure, subject,
    measured_outcome) may establish identity, so a trial that names the molecule only in an outcome DESCRIPTION,
    its summary or its eligibility text is held as a background mention while a trial MEASURING it is kept.
    Fail-open wherever the stored text cannot support a judgement (no rules for the molecule, no text at all,
    no abstract and a title that does not name the molecule, a legacy trial row not yet backfilled)."""
    if key and (molecule_id, str(key).strip().upper()) in cfg.keep:
        return Verdict(PASS, M_KEEP, reason="manual keep (registry_keep / approved benchmark / manual_pmids / gold)",
                       role=R_KEEP)
    mi = cfg.by_molecule.get(molecule_id)
    if mi is None:
        return Verdict(PASS, M_NO_RULES, reason="no identity rules for this molecule (fail-open)", role=R_UNVERIFIED)
    names = SOURCE_ZONES.get(source, ())
    roles = ZONE_ROLE.get(source, {})
    ok_roles = cfg.roles_ok.get(source, DEFAULT_ESTABLISHING.get(source, frozenset(ROLE_ORDER)))
    z = {n: str(zones.get(n, "") or "") for n in names}
    legacy = bool(zones.get("_legacy"))
    if not any(v.strip() for v in z.values()):
        return Verdict(PASS, M_INSUFFICIENT, reason="no stored text to judge (fail-open)", role=R_INSUFFICIENT)
    prose = [n for n in names if n not in INDEXING_ZONES and roles.get(n) in ok_roles]
    joined_prose = "\n".join(z[n] for n in prose)
    all_prose_zones = [n for n in names if n not in INDEXING_ZONES]
    all_prose = "\n".join(z[n] for n in all_prose_zones)

    def _held(v: Verdict) -> Verdict:
        if legacy and source == "ctgov" and v.outcome == HOLD:
            return Verdict(PASS, M_LEGACY, v.matched_term, v.zone,
                           "legacy trial row (identity fields not stored yet): not held until populated (fail-open); "
                           "would be: " + (v.reason or v.match_type), role=R_UNVERIFIED)
        return v

    # 1) canonical, then specific aliases -- identity on their own; the strongest ROLE wins
    best: Optional[Tuple[Rule, str, str]] = None
    for role in ROLE_ORDER:
        zs = [n for n in names if roles.get(n) == role]
        if not zs:
            continue
        for rules in (mi.canonical, mi.specific):
            for rule in rules:
                if source not in rule.applies_to:
                    continue
                zone = _first_zone_hit(rule, z, zs)
                if zone:
                    best = (rule, zone, role)
                    break
            if best:
                break
        if best:
            break

    # molecule-level exclusion terms veto every NON-canonical match
    veto_hit = ""
    for rule in mi.exclusions:
        if source in rule.applies_to and rule.pattern.search(joined_prose):
            veto_hit = rule.term
            break

    background: Optional[Tuple[Rule, str]] = None
    if best:
        rule, zone, role = best
        if role in ok_roles:
            if rule.role == "specific_alias" and veto_hit:
                return Verdict(EXCLUDE, M_EXCLUSION, rule.term, zone,
                               f"alias '{rule.term}' next to exclusion term '{veto_hit}'", role=R_AMBIGUOUS)
            mtype = M_CANONICAL if rule.role == "canonical" else M_SPECIFIC
            return Verdict(PASS, mtype, rule.term, zone, "", role=role)
        background = (rule, zone)

    # 2) contextual aliases: alias AND positive context, NOT a vetoing context
    ctx_unmet: List[str] = []
    for rule in mi.contextual:
        if source not in rule.applies_to:
            continue
        zone = _first_zone_hit(rule, z, prose)
        if not zone:
            continue
        # the alias must sit in an establishing zone, but its CONTEXT / veto may come from anywhere in the stored
        # prose (e.g. 'TB4' in an intervention + 'thymosin beta 4' in the summary)
        local_veto = next((v for v, p in zip(rule.veto_terms, rule.veto) if p.search(all_prose)), "")
        if local_veto or veto_hit:
            return Verdict(EXCLUDE, M_EXCLUSION, rule.term, zone,
                           f"contextual alias '{rule.term}' with exclusion term '{local_veto or veto_hit}'",
                           role=R_AMBIGUOUS)
        ctx_text = "\n".join(z[n] for n in all_prose_zones if not rule.zones or n in rule.zones)
        ctx_hit = next((c for c, p in zip(rule.context_terms, rule.context) if p.search(ctx_text)), "")
        if ctx_hit:
            return Verdict(PASS, M_CONTEXTUAL, rule.term, zone, f"context '{ctx_hit}'", role=roles.get(zone, "subject"))
        ctx_unmet.append(rule.term)
    if background is not None:
        rule, zone = background
        if zone in INDEXING_ZONES:
            return _held(Verdict(HOLD, M_INDEXING, rule.term, zone,
                                 f"'{rule.term}' only as a {zone.replace('_', ' ')} heading (no mention in the title, "
                                 f"abstract or keywords; indexing alone does not establish identity)", role="indexing"))
        return _held(Verdict(HOLD, M_BACKGROUND, rule.term, zone,
                             f"'{rule.term}' only in the {zone.replace('_', ' ')} (background mention, not the "
                             f"intervention, subject or a measured outcome)", role="background"))
    if ctx_unmet:
        return _held(Verdict(HOLD, M_CONTEXTUAL, ctx_unmet[0], "",
                             f"contextual alias '{ctx_unmet[0]}' without required context", role=R_AMBIGUOUS))

    # 3) nothing names the molecule
    abstract_zone = ABSTRACT_ZONE.get(source)
    if (abstract_zone and not z.get(abstract_zone, "").strip()
            and not any(z.get(n, "").strip() for n in SUPPORT_ZONES.get(source, ()))):
        return Verdict(PASS, M_INSUFFICIENT, reason="no abstract or indexing text and the title does not name the molecule (fail-open)",
                       role=R_INSUFFICIENT)
    return _held(Verdict(HOLD, M_NONE, reason="no identity term found in the stored text", role=R_UNRELATED))


# ---------------------------------------------------------------------------------------------
# per-source zone extraction
# ---------------------------------------------------------------------------------------------

TRIAL_FIELDS_MARKER = "identity_fields_v"
TRIAL_FIELDS_VERSION = "1"


def zones_for_trial(row: Mapping[str, object]) -> Dict[str, str]:
    """A trial row stored before the identity fields existed (no ``identity_fields_v``) is flagged ``_legacy``:
    it is judged on what it has but is never HELD on that incomplete evidence."""
    z = {k: str(row.get(k, "") or "") for k in SOURCE_ZONES["ctgov"]}
    if not row.get(TRIAL_FIELDS_MARKER):
        z["_legacy"] = "1"
    return z


_INLINE_TAG = re.compile(r"</?(?:sub|sup|i|b|em|strong|italic|bold|underline|u|span|small)\b[^>]*>", re.IGNORECASE)
_ANY_TAG = re.compile(r"<[^>]+>")


def plain_text(value: object) -> str:
    """Preprint titles/abstracts arrive as publisher markup ('VPAC<sub>1</sub>', '<h4>ABSTRACT</h4>', '&gt;'):
    inline tags are removed without a gap (so 'VPAC<sub>1</sub>' reads 'VPAC1'), block tags become a space."""
    import html
    text = _INLINE_TAG.sub("", str(value or ""))
    return html.unescape(_ANY_TAG.sub(" ", text))


def zones_for_preprint(row: Mapping[str, object]) -> Dict[str, str]:
    return {k: plain_text(row.get(k, "")) for k in SOURCE_ZONES["preprints"]}


def _flat(v: object) -> str:
    if isinstance(v, (list, tuple)):
        return "; ".join(str(x) for x in v)
    return str(v or "")


def zones_for_paper(evidence_row: Mapping[str, object], paper: Optional[Mapping[str, object]] = None) -> Dict[str, str]:
    paper = paper or {}
    out = {}
    for k in SOURCE_ZONES["pubmed"]:
        out[k] = _flat(evidence_row.get(k) or paper.get(k))
    return out


# ---------------------------------------------------------------------------------------------
# policy + bulk application
# ---------------------------------------------------------------------------------------------

def enforced(source: str, path: str = None) -> bool:
    """Is the identity gate applied to this source's published feed? config/identity_policy.csv
    (source, enforce) can switch a source off without a code change; absent file/row = enforced."""
    for r in _read_csv(path or POLICY_PATH):
        if (r.get("source") or "").strip().lower() == source:
            return _truthy(r.get("enforce", "true"))
    return True


def _zones_and_key(source: str, row: Mapping[str, object]) -> Tuple[Dict[str, str], str]:
    if source == "ctgov":
        return zones_for_trial(row), str(row.get("nct_id", "") or "")
    if source == "preprints":
        return zones_for_preprint(row), str(row.get("id", "") or "")
    return zones_for_paper(row), str(row.get("pmid", "") or "")


def filter_published(source: str, rows: Iterable[Mapping[str, object]], cfg: Optional[IdentityConfig] = None,
                     ) -> Tuple[List[Mapping[str, object]], List[Tuple[Mapping[str, object], Verdict]]]:
    """Split stored registry rows into (published, held-with-verdict). Never raises: a row that cannot be
    judged (any error) stays published. Pure function of the rows + identity rules."""
    cfg = cfg or get_config()
    kept: List[Mapping[str, object]] = []
    held: List[Tuple[Mapping[str, object], Verdict]] = []
    for r in rows:
        try:
            zones, key = _zones_and_key(source, r)
            v = evaluate(cfg, source, str(r.get("molecule_id", "") or ""), zones, key)
        except Exception:  # noqa: BLE001 -- fail-open
            kept.append(r)
            continue
        if v.published:
            kept.append(r)
        else:
            held.append((r, v))
    return kept, held


def hold_decision_fields(verdict: Verdict) -> dict:
    """Publication fields for a PubMed record held by the identity gate (same shape as
    manual_exclusions.hold_decision_fields): off the public site, still in the corpus."""
    return {
        "publication_status": "excluded_noise",
        "website_section": "",
        "auto_publish_eligible": False,
        "review_reason": f"identity {verdict.outcome}: {verdict.reason}",
        "publish_rule_id": f"identity:{verdict.outcome}",
        "display_priority": 0,
    }


def apply_identity_gate(source: str, rows: Iterable[Mapping[str, object]], report_path: str = ""
                        ) -> List[Mapping[str, object]]:
    """The feed builders' one-liner: drop rows whose identity verdict is hold/exclude (when the source is
    enforced), optionally writing a CSV of what was held and why. Fail-open on any config problem."""
    rows = list(rows)
    try:
        if not enforced(source):
            return rows
        cfg = get_config()
        kept, held = filter_published(source, rows, cfg)
    except Exception:  # noqa: BLE001
        return rows
    if report_path:
        try:
            os.makedirs(os.path.dirname(report_path) or ".", exist_ok=True)
            with open(report_path, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["source", "record_key", "molecule_id", "outcome", "match_type", "role", "matched_term",
                            "reason", "rules_version"])
                for r, v in held:
                    _, key = _zones_and_key(source, r)
                    w.writerow([source, key, r.get("molecule_id", ""), v.outcome, v.match_type, v.role,
                                v.matched_term, v.reason, cfg.version])
        except OSError:
            pass
    return kept


# ---------------------------------------------------------------------------------------------
# provenance table (written by scripts/run_identity_reeval.py; the builds recompute in memory)
# ---------------------------------------------------------------------------------------------

TABLE = "record_identity"
TABLE_COLUMNS = ("source", "record_key", "molecule_id", "outcome", "match_type", "role", "matched_term", "zone",
                 "reason", "rules_version", "evaluated_utc")


def write_verdicts(conn: sqlite3.Connection, rows: Iterable[Tuple[str, str, str, Verdict]], rules_version_: str,
                   now_iso: str, source: str) -> int:
    """Replace this source's provenance rows with a fresh full evaluation (idempotent, one transaction).
    The stored records themselves are never touched. A table from an older schema is rebuilt."""
    cols = [r[1] for r in conn.execute(f"pragma table_info({TABLE})")]
    if cols and cols != list(TABLE_COLUMNS):
        conn.execute(f"drop table {TABLE}")
    conn.execute(f"create table if not exists {TABLE} (source text, record_key text, molecule_id text, outcome text, "
                 f"match_type text, role text, matched_term text, zone text, reason text, rules_version text, "
                 f"evaluated_utc text, primary key (source, record_key, molecule_id))")
    conn.execute(f"delete from {TABLE} where source = ?", (source,))
    n = 0
    for src, key, mid, v in rows:
        conn.execute(f"insert or replace into {TABLE} values (?,?,?,?,?,?,?,?,?,?,?)",
                     (src, key, mid, v.outcome, v.match_type, v.role, v.matched_term, v.zone, v.reason,
                      rules_version_, now_iso))
        n += 1
    conn.commit()
    return n


def load_verdicts(conn: sqlite3.Connection, source: str) -> Dict[Tuple[str, str], dict]:
    cols = ("record_key", "molecule_id", "outcome", "match_type", "role", "matched_term", "zone", "reason",
            "rules_version", "evaluated_utc")
    try:
        cur = conn.execute(f"select {', '.join(cols)} from {TABLE} where source = ?", (source,))
    except sqlite3.OperationalError:
        return {}
    return {(r[0], r[1]): dict(zip(cols, r)) for r in cur}
