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
    ap.add_argument("--from-records", action="store_true", help="also snowball from every enriched record's DOI")
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

    if args.from_records:
        for r in read_jsonl(cfg["_data"] / "40_records.jsonl"):
            if r.get("doi") and not r["doi"].startswith("smashbio:"):
                dois.add(r["doi"])
        print(f"snowball seed set: {len(dois)} DOIs (seed cases + enriched records)")

    if args.snowball:
        n = 0
        # OpenAlex: references (cited) and citers of each DOI
        for d in sorted(dois):
            rl.wait()
            try:
                w = http_get(f"https://api.openalex.org/works/doi:{d}", {"mailto": cfg["contact_email"], "select": "id,referenced_works"}).json()
            except Exception:
                continue
            wid = w["id"].rsplit("/", 1)[-1]
            batch_ids = [x.rsplit("/", 1)[-1] for x in (w.get("referenced_works") or [])]
            pages = [("refs", {"filter": f"openalex_id:{'|'.join(batch_ids[i:i+50])}"}) for i in range(0, len(batch_ids), 50)]
            pages.append(("citers", {"filter": f"cites:{wid}"}))
            for rel, params in pages:
                cursor = "*"
                while cursor:
                    rl.wait()
                    try:
                        js = http_get("https://api.openalex.org/works", dict(params, **{"per-page": 200, "cursor": cursor, "mailto": cfg["contact_email"],
                                      "select": "id,doi,title,publication_year,abstract_inverted_index,primary_location,open_access,ids"})).json()
                    except Exception:
                        break
                    for x in js.get("results", []):
                        od = norm_doi(x.get("doi"))
                        if not od or od in existing:
                            continue
                        from gap_search import _inv_abstract
                        ids = x.get("ids") or {}
                        append_jsonl(hits_path, dict(source="snowball", query_tag=f"{rel}:{d}", doi=od,
                                                     pmid=(ids.get("pmid") or "").rsplit("/", 1)[-1] or None,
                                                     pmcid=(ids.get("pmcid") or "").rsplit("/", 1)[-1] or None, arxiv_id=None,
                                                     title=x.get("title") or "", abstract=_inv_abstract(x.get("abstract_inverted_index")),
                                                     year=x.get("publication_year"),
                                                     journal=((x.get("primary_location") or {}).get("source") or {}).get("display_name"),
                                                     is_oa=bool((x.get("open_access") or {}).get("is_oa")),
                                                     oa_url=(x.get("open_access") or {}).get("oa_url")))
                        existing.add(od); n += 1
                    cursor = js.get("meta", {}).get("next_cursor") if js.get("results") else None
        print(f"snowball (OpenAlex refs+citers): {n} new DOIs")
        # OpenCitations as a second net
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
