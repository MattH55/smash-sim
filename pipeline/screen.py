#!/usr/bin/env python3
"""Step 2b — deduplicate gap-search hits, drop DOIs already in SMASH-BIO and screen titles/abstracts. Writes data/20_screened.jsonl and a PRISMA count file.

Dedupe key: DOI, else PMID, else arXiv id, else normalized title.
Rule screen: config.screen.must_match_any / exclude_if_match.
Optional LLM screen on survivors: yes/no "does this paper report a pulling simulation or an analytical model
applied to SMFS data, or an explicit simulation-experiment comparison?"
Run:  python screen.py [--no-llm]
"""
from __future__ import annotations

import argparse
import json
import re

from common import extract_json_array, llm_complete, load_config, read_jsonl, write_jsonl

cfg = load_config()

SCREEN_PROMPT = """Classify each paper. Answer for each: include (true/false), category (one of: pulling_simulation, analytical_model, sim_exp_comparison, experiment_only, unrelated), reason (<=15 words).
Include = true if the paper (a) reports simulations that apply mechanical force to a biomolecule along a coordinate, OR (b) develops/applies an analytical model to single-molecule force spectroscopy data, OR (c) explicitly compares simulation and SMFS experiment. Experiment-only SMFS papers are include=false here (they are handled by SMASH-BIO).
Return a JSON array in the same order: [{"i":0,"include":true,"category":"...","reason":"..."}, ...]

PAPERS:
{papers}
"""


def dedupe(hits):
    seen, out = {}, []
    for h in hits:
        key = h["doi"] or (f"pmid:{h['pmid']}" if h["pmid"] else None) or (f"arxiv:{h['arxiv_id']}" if h["arxiv_id"] else None) \
              or "title:" + re.sub(r"\W+", "", h["title"].lower())
        if key in seen:
            seen[key]["query_tags"] = sorted(set(seen[key]["query_tags"] + [h["query_tag"]]))
            seen[key]["sources"] = sorted(set(seen[key]["sources"] + [h["source"]]))
            for f in ("doi", "pmid", "pmcid", "arxiv_id", "abstract"):
                if not seen[key].get(f) and h.get(f):
                    seen[key][f] = h[f]
            continue
        h = dict(h, query_tags=[h.pop("query_tag")], sources=[h.pop("source")], dedupe_key=key)
        seen[key] = h
        out.append(h)
    return out


def rule_screen(rec):
    text = f"{rec['title']} {rec.get('abstract') or ''}"
    for pat in cfg["screen"]["exclude_if_match"]:
        if re.search(pat, text, re.I):
            return False, f"excluded:{pat[:30]}"
    for pat in cfg["screen"]["must_match_any"]:
        if re.search(pat, text, re.I):
            return True, "rule_pass"
    return False, "no_keyword"


def llm_screen(recs, batch=20):
    for i in range(0, len(recs), batch):
        chunk = recs[i : i + batch]
        papers = "\n\n".join(f"[{j}] TITLE: {r['title']}\nABSTRACT: {(r.get('abstract') or '')[:1500]}" for j, r in enumerate(chunk))
        try:
            res = extract_json_array(llm_complete(cfg, SCREEN_PROMPT.format(papers=papers)))
            for item in res:
                r = chunk[item["i"]]
                r["llm_include"] = bool(item["include"])
                r["llm_category"] = item.get("category")
                r["llm_reason"] = item.get("reason")
        except Exception as e:
            for r in chunk:
                r["llm_include"], r["llm_category"], r["llm_reason"] = None, None, f"llm_error:{e}"
        print(f"  llm screened {min(i+batch, len(recs))}/{len(recs)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true")
    args = ap.parse_args()

    hits = read_jsonl(cfg["_data"] / "20_hits.jsonl")
    uniq = dedupe(hits)
    in_smash = {s["doi"] for s in read_jsonl(cfg["_data"] / "10_skeletons.jsonl")}
    prisma = {"records_identified": len(hits), "after_dedupe": len(uniq)}
    uniq = [u for u in uniq if u.get("doi") not in in_smash]
    prisma["already_in_smash_bio"] = prisma["after_dedupe"] - len(uniq)

    for r in uniq:
        r["rule_include"], r["rule_reason"] = rule_screen(r)
    rule_pass = [r for r in uniq if r["rule_include"]]
    prisma["after_rule_screen"] = len(rule_pass)

    if cfg["screen"].get("llm_screen") and not args.no_llm:
        llm_screen(rule_pass)
        final = [r for r in rule_pass if r.get("llm_include")]
    else:
        final = rule_pass
    prisma["after_llm_screen"] = len(final)

    write_jsonl(cfg["_data"] / "20_all_dedup.jsonl", uniq)
    write_jsonl(cfg["_data"] / "20_screened.jsonl", final)
    (cfg["_data"] / "prisma_counts.json").write_text(json.dumps(prisma, indent=2))
    print(json.dumps(prisma, indent=2))
