#!/usr/bin/env python3
"""Step 2c (optional) — push the seed cases (seed/seed_cases.csv, from the literature report) into the search set
so they are always extracted, and write a snowball list of their DOIs for Crossref citation chasing.

Run:  python seed.py [--snowball]
--snowball: for each seed DOI, pull 'is-referenced-by' and 'references' via OpenCitations and append to data/20_hits.jsonl
"""
from __future__ import annotations

import argparse
import csv

from common import RateLimiter, append_jsonl, http_get, load_config, norm_doi, read_jsonl

cfg = load_config()


def crossref_meta(doi: str) -> dict | None:
    try:
        m = http_get(f"https://api.crossref.org/works/{doi}", headers={"User-Agent": f"smash-sim (mailto:{cfg['contact_email']})"}).json()["message"]
    except Exception:
        return None
    return dict(doi=doi, title=(m.get("title") or [""])[0], abstract=m.get("abstract", ""),
                year=(m.get("issued", {}).get("date-parts") or [[None]])[0][0],
                journal=(m.get("container-title") or [""])[0], pmid=None, pmcid=None, arxiv_id=None, is_oa=False)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--snowball", action="store_true")
    args = ap.parse_args()

    hits_path = cfg["_data"] / "20_hits.jsonl"
    existing = {h["doi"] for h in read_jsonl(hits_path) if h.get("doi")}
    rl = RateLimiter(2)

    with open(cfg["_root"] / "seed" / "seed_cases.csv", newline="") as f:
        seeds = list(csv.DictReader(f))
    dois = {norm_doi(s[k]) for s in seeds for k in ("sim_doi", "exp_doi") if s.get(k)}
    added = 0
    for d in sorted(dois):
        if d in existing:
            continue
        rl.wait()
        m = crossref_meta(d)
        if m:
            append_jsonl(hits_path, dict(m, source="seed", query_tag="seed"))
            added += 1
    print(f"seed: {added} new DOIs appended to {hits_path}")

    if args.snowball:
        n = 0
        for d in sorted(dois):
            for rel in ("citations", "references"):
                rl.wait()
                try:
                    js = http_get(f"https://opencitations.net/index/api/v1/{rel}/{d}").json()
                except Exception:
                    continue
                for row in js:
                    other = norm_doi(row.get("citing" if rel == "citations" else "cited"))
                    if other and other not in existing:
                        rl.wait()
                        m = crossref_meta(other)
                        if m:
                            append_jsonl(hits_path, dict(m, source="snowball", query_tag=f"{rel}:{d}"))
                            existing.add(other)
                            n += 1
        print(f"snowball: {n} new DOIs appended (screen them with screen.py)")
