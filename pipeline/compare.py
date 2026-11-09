#!/usr/bin/env python3
"""Step 5 — build per-observable comparison rows between enriched simulation records and SMFS experiments.

Candidate pairs come from three sources, in priority order:
  1. SMASH-BIO's own match table (data/smash/sim_matches.csv: same_construct_same_geometry > same_molecule_different_geometry > homolog)
  2. experiment DOIs the simulation paper itself cites (experimental_comparison_in_paper.experiment_dois), resolved to SMFS ids by DOI
  3. for SIMX (gap) records only: PDB / UniProt identity against SMFS records
For every simulation record that has >=1 candidate or an in-paper comparison, the comparison prompt is run once with
all candidates; it returns one row per observable actually compared.

Writes data/50_pairs.jsonl, data/50_comparisons.jsonl, data/50_rejected.jsonl.   Idempotent per sim_id.
Run:  python compare.py [--no-llm] [--limit N]
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
from pathlib import Path

import jsonschema

from common import ROOT, append_jsonl, extract_json_array, llm_complete, load_config, load_schema, next_id, norm_doi, read_jsonl, write_jsonl

cfg = load_config()
CMP_SCHEMA = load_schema("comparison")
PROMPT = (ROOT / "prompts" / "extract_comparison.md").read_text()
MAX_CHARS = int(cfg["llm"].get("max_prompt_chars", 300_000))
LEVEL_RANK = {"same_construct_same_geometry": 0, "same_molecule_different_geometry": 1, "homolog": 2}
MAX_CANDIDATES = 8


def load_smfs() -> dict[str, dict]:
    out = {}
    with gzip.open(cfg["_data"] / "smash" / "smfs_records.jsonl.gz", "rt") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                out[r["record_id"]] = r
    return out


def smfs_slim(r: dict) -> dict:
    """The parts of an SMFS record the comparison prompt needs (keeps the prompt small)."""
    pub = r.get("publication", {})
    return {
        "smfs_id": r["record_id"], "doi": pub.get("doi"), "title": pub.get("title"), "year": pub.get("year"),
        "technique": r.get("technique") or r.get("experiment", {}).get("technique"),
        "system": r.get("system"), "protocol": r.get("protocol") or r.get("experiment", {}).get("protocol"),
        "results": r.get("results"), "abstract": (pub.get("abstract") or "")[:2500],
    }


def candidates(sim: dict, matches: list[dict], smfs: dict[str, dict], by_doi: dict[str, str]) -> list[dict]:
    base_id = sim["sim_id"].split(".")[0]
    cands = {}
    for m in matches:
        if m["sim_record_id"] == base_id and m["experimental_source"] == "SMASH-BIO" and m["experimental_record_id"] in smfs:
            cands[m["experimental_record_id"]] = dict(smfs_id=m["experimental_record_id"], how=m["match_level"],
                                                      rank=LEVEL_RANK.get(m["match_level"], 3), geometry_match=m.get("geometry_match"))
    for d in sim.get("experimental_comparison_in_paper", {}).get("experiment_dois", []):
        sid = by_doi.get(norm_doi(d))
        if sid:
            cands[sid] = dict(smfs_id=sid, how="cited_doi", rank=-1, geometry_match=None)
        else:
            cands[f"doi:{d}"] = dict(smfs_id=None, exp_doi=d, how="cited_doi_unindexed", rank=0, geometry_match=None)
    if sim["sim_id"].startswith("SIMX"):
        pdb, uni = (sim["molecule"].get("pdb_id") or "").lower(), (sim["molecule"].get("uniprot_or_rfam") or "").lower()
        for sid, r in smfs.items():
            ids = {(i.get("scheme"), (i.get("id") or "").lower()) for e in r.get("system", {}).get("entities", []) for i in e.get("identifiers", [])}
            if (pdb and ("pdb", pdb) in ids) or (uni and (("uniprot", uni) in ids or ("rfam", uni) in ids)):
                cands.setdefault(sid, dict(smfs_id=sid, how="identifier", rank=1, geometry_match=None))
    ranked = sorted(cands.values(), key=lambda c: c["rank"])
    return ranked[:MAX_CANDIDATES]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()

    sims = read_jsonl(cfg["_data"] / "40_records.jsonl")
    smfs = load_smfs()
    by_doi = {norm_doi(r.get("publication", {}).get("doi")): sid for sid, r in smfs.items() if r.get("publication", {}).get("doi")}
    with open(cfg["_data"] / "smash" / "sim_matches.csv", newline="") as f:
        matches = list(csv.DictReader(f))
    ft = {x.get("doi") or x["key"]: x["path"] for x in read_jsonl(cfg["_data"] / "30_fulltext_index.jsonl")}

    pairs = []
    for s in sims:
        for c in candidates(s, matches, smfs, by_doi):
            pairs.append(dict(sim_id=s["sim_id"], **c))
    write_jsonl(cfg["_data"] / "50_pairs.jsonl", pairs)
    print(f"{len(pairs)} candidate pairs for {len({p['sim_id'] for p in pairs})} simulation records")
    if args.no_llm:
        raise SystemExit

    out = cfg["_data"] / "50_comparisons.jsonl"
    done = {c["sim_id"] for c in read_jsonl(out)} | {c.get("sim_id") for c in read_jsonl(cfg["_data"] / "50_rejected.jsonl")}
    ids = [c["cmp_id"] for c in read_jsonl(out)]
    todo = [s for s in sims if s["sim_id"] not in done
            and (any(p["sim_id"] == s["sim_id"] for p in pairs) or s.get("experimental_comparison_in_paper", {}).get("present"))]
    if args.limit:
        todo = todo[: args.limit]
    print(f"{len(todo)} records to compare")
    for s in todo:
        my = [p for p in pairs if p["sim_id"] == s["sim_id"]]
        exps = [smfs_slim(smfs[p["smfs_id"]]) for p in my if p.get("smfs_id")]
        for p in my:
            if not p.get("smfs_id"):
                exps.append({"smfs_id": None, "doi": p.get("exp_doi"), "note": "cited by the simulation paper; not indexed in SMASH-BIO"})
        text = Path(ft[s["doi"]]).read_text()[:MAX_CHARS] if s["doi"] in ft else ""
        prompt = (PROMPT.replace("{{sim_record_json}}", json.dumps(s, ensure_ascii=False))
                        .replace("{{smfs_records_json}}", json.dumps(exps, ensure_ascii=False))
                        .replace("{{doi}}", s["doi"]).replace("{{full_text}}", text))
        try:
            rows = extract_json_array(llm_complete(cfg, prompt))
        except Exception as e:
            append_jsonl(cfg["_data"] / "50_rejected.jsonl", {"sim_id": s["sim_id"], "errors": [f"llm/parse: {e}"]})
            continue
        n_ok = 0
        for r in rows:
            r["sim_id"] = s["sim_id"]
            r["cmp_id"] = next_id("CMP", ids)
            r.setdefault("curation", {})["status"] = "auto"
            errs = [e.message for e in jsonschema.Draft202012Validator(CMP_SCHEMA).iter_errors(r)]
            if errs:
                append_jsonl(cfg["_data"] / "50_rejected.jsonl", {"sim_id": s["sim_id"], "errors": errs, "row": r})
            else:
                ids.append(r["cmp_id"]); append_jsonl(out, r); n_ok += 1
        print(f"  {s['sim_id']}: {n_ok}/{len(rows)} comparison rows")
