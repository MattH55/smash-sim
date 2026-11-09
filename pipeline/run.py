#!/usr/bin/env python3
"""Orchestrator.  python run.py --steps ingest,fulltext,enrich,compare,views  [--limit N] [--no-llm]

Steps: ingest | gap_search | screen | seed | fulltext | enrich | compare | views | review_export
"""
import argparse, subprocess, sys
from pathlib import Path

HERE = Path(__file__).parent
STEP_CMD = {
    "ingest": ["ingest_smash.py"],
    "gap_search": ["gap_search.py"],
    "seed": ["seed.py", "--snowball"],
    "screen": ["screen.py"],
    "fulltext": ["fetch_fulltext.py"],
    "enrich": ["enrich.py"],
    "compare": ["compare.py"],
    "views": ["views.py"],
    "review_export": ["review_sheet.py", "export"],
}
ap = argparse.ArgumentParser()
ap.add_argument("--steps", default="ingest,fulltext,enrich,compare,views")
ap.add_argument("--limit", type=int)
ap.add_argument("--no-llm", action="store_true")
a = ap.parse_args()
for step in a.steps.split(","):
    cmd = [sys.executable, str(HERE / STEP_CMD[step][0])] + STEP_CMD[step][1:]
    if a.limit and step in ("enrich", "compare"):
        cmd += ["--limit", str(a.limit)]
    if a.no_llm and step in ("screen", "compare"):
        cmd += ["--no-llm"]
    print(f"\n=== {step}: {' '.join(cmd[1:])}", flush=True)
    subprocess.run(cmd, check=True, cwd=HERE)
