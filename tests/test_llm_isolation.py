#!/usr/bin/env python3
"""Production LLM-isolation policy test.

Production code (the pipeline package, scripts, top-level entry points and CI
workflows) must not import LLM SDKs or import/invoke the experimental LLM tool
(scripts/experimental_llm_extract.py). Only that script and tests may reference it.

Plain script (no pytest): run `python3 tests/test_llm_isolation.py`.
Includes a negative test proving the guard fails when a forbidden import is injected.
"""

from __future__ import annotations

import ast
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EXPERIMENTAL = os.path.join("scripts", "experimental_llm_extract.py")
FORBIDDEN_MODULES = {
    "anthropic", "openai", "ollama", "litellm", "langchain", "langchain_core",
    "langchain_openai", "llama_index", "llama_cpp", "cohere", "mistralai",
    "google.generativeai", "google.genai", "groq", "transformers", "vllm",
    "experimental_llm_extract",
}
# Text patterns for non-Python files (workflows, shell) and string-based invocation.
FORBIDDEN_TEXT = re.compile(r"experimental_llm_extract")

_PASS = 0
_FAIL = 0


def check(name, cond):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
    else:
        _FAIL += 1
        print(f"  FAIL: {name}")


def production_files(root):
    """Yield (relpath, kind) for production sources under root."""
    for top in ("retarats_pipeline", "scripts", ".github/workflows"):
        for dp, dns, fns in os.walk(os.path.join(root, top)):
            dns[:] = [d for d in dns if d != "__pycache__"]
            for fn in fns:
                if fn.endswith((".py", ".yml", ".yaml", ".sh")):
                    yield os.path.relpath(os.path.join(dp, fn), root)
    for fn in sorted(os.listdir(root)):
        if fn.endswith((".py", ".sh", ".command")) and os.path.isfile(os.path.join(root, fn)):
            yield fn


def _forbidden(modname):
    parts = modname.split(".")
    return any(".".join(parts[: i + 1]) in FORBIDDEN_MODULES for i in range(len(parts))) \
        or parts[-1] in FORBIDDEN_MODULES


def violations(root):
    out = []
    for rel in production_files(root):
        if rel == EXPERIMENTAL:
            continue
        with open(os.path.join(root, rel), encoding="utf-8", errors="replace") as fh:
            src = fh.read()
        if rel.endswith(".py"):
            try:
                tree = ast.parse(src)
            except SyntaxError:
                out.append((rel, "unparseable"))
                continue
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    base = node.module or ""
                    mods = [base] + [f"{base}.{a.name}" for a in node.names]
                elif isinstance(node, ast.Call) and getattr(node.func, "id", "") == "__import__" \
                        and node.args and isinstance(node.args[0], ast.Constant) \
                        and isinstance(node.args[0].value, str):
                    mods = [node.args[0].value]
                for m in mods:
                    if m and _forbidden(m):
                        out.append((rel, f"import {m}"))
        # String-based invocation (subprocess, importlib, workflow steps, shell).
        if FORBIDDEN_TEXT.search(src) and not (rel.endswith(".py") and _only_in_comments(src)):
            out.append((rel, "references experimental_llm_extract"))
    return out


def _only_in_comments(src):
    for line in src.splitlines():
        if FORBIDDEN_TEXT.search(line) and not line.lstrip().startswith("#"):
            return False
    return True


def test_production_is_clean():
    v = violations(ROOT)
    for rel, why in v:
        print(f"    {rel}: {why}")
    check("no production file imports LLM SDKs or the experimental tool", not v)


def test_experimental_header_present():
    with open(os.path.join(ROOT, EXPERIMENTAL), encoding="utf-8") as fh:
        head = fh.read(3000)
    check("experimental script carries isolation-policy header", "ISOLATION POLICY" in head
          and "isolated experimental research tooling" in head)


def test_negative_injection():
    """The guard must fail when a forbidden import / reference is injected."""
    cases = {
        "retarats_pipeline/inj_sdk.py": "import anthropic\n",
        "retarats_pipeline/inj_from.py": "from openai import OpenAI\n",
        "scripts/inj_exp.py": "from scripts.experimental_llm_extract import main\n",
        "scripts/inj_dyn.py": "import subprocess\nsubprocess.run(['python3','scripts/experimental_llm_extract.py'])\n",
        ".github/workflows/inj.yml": "steps:\n  - run: python3 scripts/experimental_llm_extract.py\n",
    }
    for rel, body in cases.items():
        with tempfile.TemporaryDirectory() as tmp:
            for d in ("retarats_pipeline", "scripts", ".github/workflows"):
                os.makedirs(os.path.join(tmp, d))
            with open(os.path.join(tmp, "scripts", "experimental_llm_extract.py"), "w") as fh:
                fh.write("import openai\n")  # allowed: the experimental script itself
            check(f"clean tree passes ({rel})", not violations(tmp))
            with open(os.path.join(tmp, rel), "w") as fh:
                fh.write(body)
            check(f"injected violation is caught ({rel})", bool(violations(tmp)))


def main():
    test_production_is_clean()
    test_experimental_header_present()
    test_negative_injection()
    print(f"{_PASS} passed, {_FAIL} failed")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
