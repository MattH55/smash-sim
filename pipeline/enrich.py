#!/usr/bin/env python3
"""Step 4 — LLM enrichment of skeleton records (from SMASH-BIO) and extraction of new records (from gap search).

For each skeleton in data/10_skeletons.jsonl with full text available, the model receives the skeleton and the
paper and returns the completed record(s). Rules enforced here, not just in the prompt:
  * sim_id, doi, molecule.pdb_id and molecule.uniprot_or_rfam from the skeleton are authoritative and restored
    if the model changes them (SMASH-BIO identifiers are resolver-verified).
  * a skeleton may be split into several records if the paper has several (molecule, class, protocol) setups;
    extra records get ids SIM-<base>.2, .3 ... until SMASH-BIO assigns real ones.
  * class_id must leave UNRESOLVED.
For gap-search papers (no skeleton) the plain extraction prompt is used and ids are provisional SIMX-######.

Writes data/40_records.jsonl (valid) and data/40_rejected.jsonl.  Idempotent per sim_id / doi.
Run:  python enrich.py [--limit N] [--only-unresolved] [--skip-gap]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import jsonschema

from coerce import coerce, fix_impl
from common import (ROOT, append_jsonl, extract_json_array, llm_complete, load_config, load_schema, load_vocab,
                    next_id, read_jsonl, sha1)

cfg = load_config()
SCHEMA = load_schema("simulation_record")
CLASSES = load_vocab("model_class")
IMPLS = load_vocab("implementation")
P_ENRICH = (ROOT / "prompts" / "enrich_simulation_record.md").read_text()
P_EXTRACT = (ROOT / "prompts" / "extract_simulation_record.md").read_text()
MAX_CHARS = int(cfg["llm"].get("max_prompt_chars", 300_000))
LOCKED = ["sim_id", "doi", ("molecule", "pdb_id"), ("molecule", "uniprot_or_rfam"), "_smashbio"]


def validate(rec: dict) -> list[str]:
    errs = [e.message for e in jsonschema.Draft202012Validator(SCHEMA).iter_errors(rec)]
    if rec.get("class_id") not in CLASSES:
        errs.append(f"class_id {rec.get('class_id')} not in vocab (UNRESOLVED not allowed after enrichment)")
    if rec.get("impl_id") and (rec["impl_id"] not in IMPLS or rec["impl_id"] == "ALIASES"):
        errs.append(f"impl_id {rec.get('impl_id')} not in vocab")
    prov = rec.get("_prov", {})
    for grp in ("protocol", "observables", "benchmark", "sampling"):
        for k, v in (rec.get(grp) or {}).items():
            if isinstance(v, (int, float)) and not isinstance(v, bool) and f"{grp}.{k}" not in prov:
                errs.append(f"missing _prov for {grp}.{k}")
    return errs


def restore_locked(rec: dict, skel: dict, suffix: int | None) -> dict:
    for key in LOCKED:
        if isinstance(key, tuple):
            g, k = key
            if skel.get(g, {}).get(k):
                rec.setdefault(g, {})[k] = skel[g][k]
        elif key in skel and skel[key] is not None:
            rec[key] = skel[key]
    if suffix:
        rec["sim_id"] = f"{skel['sim_id']}.{suffix}"
    return rec


import re as _re
_VEL = _re.compile(r"(\d+(?:\.\d+)?(?:\s*[×x]\s*10\^?-?\d+)?)\s*(Å|A|angstrom|nm|µm|um|m)\s*(?:/|per)\s*(fs|ps|ns|µs|us|ms|s)\b", _re.I)
_LEN = {"å": 1e-10, "a": 1e-10, "angstrom": 1e-10, "nm": 1e-9, "µm": 1e-6, "um": 1e-6, "m": 1.0}
_TIME = {"fs": 1e-15, "ps": 1e-12, "ns": 1e-9, "µs": 1e-6, "us": 1e-6, "ms": 1e-3, "s": 1.0}
_SPR = _re.compile(r"(\d+(?:\.\d+)?)\s*(kJ|kcal)\s*(?:/|·|\s)?\s*mol(?:\^?-1|–1|⁻¹)?\s*(?:/|·|\s)?\s*(nm|Å|A)\s*(?:\^?-?2|²|2)", _re.I)
_SPR2 = _re.compile(r"(\d+(?:\.\d+)?)\s*(pN\s*/\s*nm|N\s*/\s*m)", _re.I)


def _num(x):
    x = x.replace(" ", "").replace("×", "x").lower()
    if "x10" in x:
        a, b = x.split("x10"); return float(a) * 10 ** float(b.replace("^", ""))
    return float(x)


def verify_units(rec: dict) -> None:
    """Re-derive velocity and spring constant from the provenance quote; overwrite on >10% mismatch and log it."""
    prov, log, p = rec.get("_prov", {}), rec.setdefault("_coerced", {}), rec.get("protocol", {})
    q = (prov.get("protocol.pulling_velocity_m_s") or {}).get("quote", "")
    m = _VEL.search(q)
    if m and p.get("pulling_velocity_m_s"):
        v = _num(m.group(1)) * _LEN[m.group(2).lower()] / _TIME[m.group(3).lower()]
        if abs(v - p["pulling_velocity_m_s"]) > 0.1 * v:
            log["protocol.pulling_velocity_m_s"] = f"extractor {p['pulling_velocity_m_s']} -> re-parsed {v:g} m/s from quote"
            p["pulling_velocity_m_s"] = v
    q = (prov.get("protocol.spring_constant_pN_nm") or {}).get("quote", "")
    m = _SPR.search(q)
    k = None
    if m:
        val, en, ln = _num(m.group(1)), m.group(2).lower(), m.group(3).lower()
        k = val * (1.66 if en == "kj" else 6.95) * (1.0 if ln == "nm" else 100.0)   # kJ/mol/nm2 -> 1.66; kcal/mol/A2 -> 695
    else:
        m2 = _SPR2.search(q)
        if m2:
            k = _num(m2.group(1)) * (1000.0 if m2.group(2).lower().startswith("n") else 1.0)
    if k and p.get("spring_constant_pN_nm") and abs(k - p["spring_constant_pN_nm"]) > 0.1 * k:
        log["protocol.spring_constant_pN_nm"] = f"extractor {p['spring_constant_pN_nm']} -> re-parsed {k:g} pN/nm from quote"
        p["spring_constant_pN_nm"] = k


def postprocess(rec: dict) -> dict:
    rec = coerce(rec, SCHEMA)
    verify_units(rec)
    fix_impl(rec, set(IMPLS) - {"ALIASES"}, rec.setdefault("_coerced", {}))
    prov = rec.setdefault("_prov", {})
    s = rec.get("sampling") or {}
    # total sampled time is arithmetic, not extraction: derive it and record that
    if s.get("n_trajectories") and s.get("trajectory_length_ns") and "sampling.total_sampled_time_ns" not in prov:
        s["total_sampled_time_ns"] = round(s["n_trajectories"] * s["trajectory_length_ns"], 3)
        prov["sampling.total_sampled_time_ns"] = {"quote": "derived: n_trajectories * trajectory_length_ns", "page": None, "confidence": 0.8}
    # any other unsupported number: keep it, but flag for review at confidence 0.2 rather than rejecting the record
    for grp in ("protocol", "observables", "benchmark", "sampling"):
        for k, v in (rec.get(grp) or {}).items():
            if isinstance(v, (int, float)) and not isinstance(v, bool) and f"{grp}.{k}" not in prov:
                prov[f"{grp}.{k}"] = {"quote": "UNSUPPORTED: extractor supplied no quote; verify", "page": None, "confidence": 0.2}
    b = rec.get("benchmark") or {}
    if b.get("benchmark_source", "absent") == "absent" and any(b.get(k) for k in ("hardware", "wall_time_per_trajectory_h", "throughput_ns_per_day", "core_hours_total", "gpu_hours_total")):
        b["benchmark_source"] = "reported"
    mol = rec.get("molecule", {})
    if mol.get("sequence") and not mol.get("sequence_sha1"):
        mol["sequence_sha1"] = sha1(mol["sequence"].upper())
    rec.setdefault("curation", {})["status"] = "auto"
    b, s = rec.get("benchmark") or {}, rec.get("sampling") or {}
    if b.get("wall_time_per_trajectory_h") is None and b.get("throughput_ns_per_day") and s.get("trajectory_length_ns"):
        b["wall_time_per_trajectory_h"] = round(24 * s["trajectory_length_ns"] / b["throughput_ns_per_day"], 3)
        b["benchmark_source"] = "derived"
        rec.setdefault("_prov", {})["benchmark.wall_time_per_trajectory_h"] = {
            "quote": "derived: 24*trajectory_length_ns/throughput_ns_per_day", "page": None, "confidence": 0.8}
    rec["benchmark"] = b
    # velocity gap vs AFM reference (1e-6 m/s) for quick views
    v = rec.get("protocol", {}).get("pulling_velocity_m_s")
    if v:
        rec.setdefault("_derived", {})["log10_velocity_over_1um_s"] = round(__import__("math").log10(v / 1e-6), 2)
    return rec


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--only-unresolved", action="store_true", help="enrich only skeletons with class UNRESOLVED")
    ap.add_argument("--skip-gap", action="store_true", help="do not extract gap-search papers")
    args = ap.parse_args()

    out_ok, out_bad = cfg["_data"] / "40_records.jsonl", cfg["_data"] / "40_rejected.jsonl"
    done_ids = {r["sim_id"].split(".")[0] for r in read_jsonl(out_ok)}        # rejected records are retried
    done_dois = {r.get("doi") for r in read_jsonl(out_ok)}
    ids = [r["sim_id"] for r in read_jsonl(out_ok)]
    ft = {x.get("doi") or x["key"]: x["path"] for x in read_jsonl(cfg["_data"] / "30_fulltext_index.jsonl")}

    # --- skeletons (SMASH-BIO records) ---
    skels = [s for s in read_jsonl(cfg["_data"] / "10_skeletons.jsonl") if s["sim_id"] not in done_ids]
    if args.only_unresolved:
        skels = [s for s in skels if s["class_id"] == "UNRESOLVED"]
    no_ft = [s["sim_id"] for s in skels if s["doi"] not in ft]
    skels = [s for s in skels if s["doi"] in ft]
    print(f"{len(no_ft)} skeletons skipped (no full text)")
    if args.limit:
        skels = skels[: args.limit]
    print(f"{len(skels)} skeletons to enrich")
    n_done = 0
    for skel in skels:
        text = Path(ft[skel["doi"]]).read_text()[:MAX_CHARS]
        prompt = (P_ENRICH.replace("{{skeleton_json}}", json.dumps(skel, ensure_ascii=False))
                          .replace("{{doi}}", skel["doi"]).replace("{{full_text}}", text))
        try:
            recs = extract_json_array(llm_complete(cfg, prompt))
        except Exception as e:
            append_jsonl(out_bad, {"sim_id": skel["sim_id"], "doi": skel["doi"], "errors": [f"llm/parse: {e}"]})
            print(f"  {skel['sim_id']}: LLM/parse error: {str(e)[:300]}")
            continue
        for i, rec in enumerate(recs):
            rec = postprocess(restore_locked(rec, skel, i if i else None))
            errs = validate(rec)
            if errs:
                append_jsonl(out_bad, {"sim_id": rec["sim_id"], "doi": skel["doi"], "errors": errs, "record": rec})
                print(f"  {skel['sim_id']}: REJECTED ({len(errs)} schema errors): {'; '.join(errs[:4])[:400]}")
            else:
                append_jsonl(out_ok, rec); ids.append(rec["sim_id"]); n_done += 1
        print(f"  {skel['sim_id']}: {len(recs)} record(s) returned")

    # --- gap-search papers (not in SMASH-BIO) ---
    if not args.skip_gap:
        gap = [x for x in read_jsonl(cfg["_data"] / "30_fulltext_index.jsonl")
               if x.get("source_set") == "gap" and (x.get("doi") or x["key"]) not in done_dois]
        if args.limit:
            gap = gap[: max(0, args.limit - len(skels))]
        print(f"{len(gap)} gap-search papers to extract")
        for item in gap:
            doi = item.get("doi") or item["key"]
            prompt = P_EXTRACT.replace("{{doi}}", doi).replace("{{full_text}}", Path(item["path"]).read_text()[:MAX_CHARS])
            try:
                recs = extract_json_array(llm_complete(cfg, prompt))
            except Exception as e:
                append_jsonl(out_bad, {"doi": doi, "errors": [f"llm/parse: {e}"]}); continue
            for rec in recs:
                rec["doi"] = rec.get("doi") or doi
                rec["sim_id"] = next_id("SIMX", [i for i in ids if i.startswith("SIMX")])
                rec = postprocess(rec)
                errs = validate(rec)
                if errs:
                    append_jsonl(out_bad, {"doi": doi, "errors": errs, "record": rec})
                else:
                    append_jsonl(out_ok, rec); ids.append(rec["sim_id"]); n_done += 1
            print(f"  gap {doi}: {len(recs)} record(s)")
    print(f"done: {n_done} valid records written to {out_ok}")
