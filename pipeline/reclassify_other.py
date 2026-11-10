#!/usr/bin/env python3
"""Step 5b — clean the comparison table: drop rows that are not comparisons, and re-label observable='other'
rows into the controlled observable vocabulary with a cheap batched LLM call. Idempotent; rewrites 50_comparisons.jsonl.
Run:  python reclassify_other.py
"""
from __future__ import annotations
import json, re
from common import extract_json_array, llm_complete, load_config, load_schema, read_jsonl, write_jsonl

cfg = load_config()
OBS = [o for o in load_schema("comparison")["properties"]["observable"]["enum"] if o != "other"]
NOT_A_COMPARISON = re.compile(r"no (experimental )?comparison|not compared|no experiment|without experimental|no direct comparison", re.I)

PROMPT = """Each item is a comparison between a biomolecular pulling simulation and an experiment, currently labelled observable="other".
Choose the best observable from this list: {obs}. Use "other" only if none fits. Also say drop=true if the item is NOT actually a
comparison of a quantity or qualitative feature between simulation and experiment (e.g. "no experimental comparison made").
Return a JSON array in the same order: [{{"i":0,"observable":"...","drop":false}}, ...]

ITEMS:
{items}
"""

if __name__ == "__main__":
    path = cfg["_data"] / "50_comparisons.jsonl"
    rows = read_jsonl(path)
    keep, todo = [], []
    for r in rows:
        blob = f"{r.get('note') or ''} {r.get('verdict_criterion') or ''}"
        if NOT_A_COMPARISON.search(blob) and r.get("match_level") not in ("UNTESTED", "PRED"):
            continue                                   # dropped
        if r.get("observable") == "other" and not (r.get("_coerced") or {}).get("observable_reclassified"):
            todo.append(r)
        keep.append(r)
    print(f"{len(rows)} rows -> {len(keep)} after dropping non-comparisons; {len(todo)} 'other' rows to reclassify")
    for i in range(0, len(todo), 25):
        chunk = todo[i:i+25]
        items = "\n".join(f"[{j}] sim={str(r.get('sim_value'))[:80]!r} exp={str(r.get('exp_value'))[:80]!r} units={r.get('units')} "
                          f"criterion={str(r.get('verdict_criterion'))[:120]!r} note={str(r.get('note'))[:160]!r}" for j, r in enumerate(chunk))
        try:
            res = extract_json_array(llm_complete(cfg, PROMPT.replace("{obs}", ", ".join(OBS)).replace("{items}", items)))
        except Exception as e:
            print("  reclassify batch failed:", str(e)[:120]); continue
        for it in res:
            try:
                r = chunk[int(it["i"])]
            except Exception:
                continue
            r.setdefault("_coerced", {})["observable_reclassified"] = f"other -> {it.get('observable')} (drop={it.get('drop')})"
            if it.get("drop"):
                r["_drop"] = True
            elif it.get("observable") in OBS:
                r["observable"] = it["observable"]
        print(f"  reclassified {min(i+25, len(todo))}/{len(todo)}")
    keep = [r for r in keep if not r.get("_drop")]
    write_jsonl(path, keep)
    print(f"written {len(keep)} rows")
