#!/usr/bin/env python3
from __future__ import annotations

import runpy
import sys
from pathlib import Path

if __name__ == "__main__":
    script = Path(__file__).with_name("run_enrichment_pipeline.py")
    sys.argv = [str(script), "--mode", "basic", *sys.argv[1:]]
    runpy.run_path(str(script), run_name="__main__")
