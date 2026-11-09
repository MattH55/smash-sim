#!/usr/bin/env python3
"""Step 4b — cross-model verification of extracted numbers.

For every enriched record not yet verified, a *different* model is shown each numeric field together with the
quote the extractor cited, and asked: does the quote support this value (after unit conversion)? It returns, per
field, supported yes/no/unclear and its own parsed value. On "no", the field's confidence is set to 0.25 and the
verifier's value is recorded in _verify so the review sheet shows both; on "unclear" confidence is capped at 0.5.
Nothing is overwritten automatically: a human decides from the review sheet.

Cheap: the prompt carries only (field, value, quote) triples, not the paper.
Run:  python verify.py [--limit N]
"""
from __future__ import annotations

import argparse
import copy
import json

from common import extract_json_array, llm_complete, load_config, read_jsonl, write_jsonl

cfg = load_config()
VERIFIER = cfg["llm"].get("verifier_model") or cfg["llm"].get("fallback_models", [cfg["llm"]["model"]])[0]

PROMPT = """You are checking numbers extracted from a scientific paper on biomolecular pulling simulations.
For each item you get the field name (with its unit), the extracted value, and the quote the extractor cited.
Decide whether the quote supports the value. Allowed unit conversions: 1 Å/ps = 100 m/s; 1 nm/ns = 1 m/s; 1 Å/ns = 0.1 m/s;
1 kcal mol^-1 Å^-2 = 695 pN/nm; 1 kJ mol^-1 nm^-2 = 1.66 pN/nm; 1 N/m = 1000 pN/nm; 1 kcal/mol = 1.69 kBT (298 K).
A quote that says "derived: ..." describes arithmetic from other fields; mark it "derived".
Return a JSON array, same order, items: {"i": <index>, "supported": "yes"|"no"|"unclear"|"derived", "parsed_value": <number or null>, "why": "<=12 words"}

ITEMS:
{items}
"""

NUMERIC_GROUPS = ("protocol", "sampling", "benchmark", "observables")


def items_for(rec: dict) -> list[tuple[str, float, str]]:
    out = []
    for g in NUMERIC_GROUPS:
        for k, v in (rec.get(g) or {}).items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                q = (rec.get("_prov", {}).get(f"{g}.{k}") or {}).get("quote", "")
                out.append((f"{g}.{k}", v, q))
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()
    path = cfg["_data"] / "40_records.jsonl"
    recs = read_jsonl(path)
    todo = [r for r in recs if not r.get("_verify")]
    if args.limit:
        todo = todo[: args.limit]
    print(f"verifier model: {VERIFIER}; {len(todo)} records to verify")
    vcfg = copy.deepcopy(cfg)
    vcfg["llm"]["model"] = VERIFIER
    vcfg["llm"]["fallback_models"] = [m for m in cfg["llm"].get("fallback_models", []) if m != VERIFIER]
    vcfg["llm"]["max_tokens"] = 3000
    n_flag = 0
    for r in todo:
        items = items_for(r)
        if not items:
            r["_verify"] = {"model": VERIFIER, "checked": 0, "flagged": 0}
            continue
        listing = "\n".join(f"[{i}] {f} = {v} | quote: {q[:280]!r}" for i, (f, v, q) in enumerate(items))
        try:
            res = extract_json_array(llm_complete(vcfg, PROMPT.replace("{items}", listing)))
        except Exception as e:
            print(f"  {r['sim_id']}: verifier error {str(e)[:120]}")
            continue
        flagged = {}
        for it in res:
            try:
                f, v, q = items[int(it["i"])]
            except Exception:
                continue
            sup = it.get("supported")
            prov = r.setdefault("_prov", {}).setdefault(f, {})
            if sup == "no":
                prov["confidence"] = min(prov.get("confidence", 1.0), 0.25)
                flagged[f] = {"verifier_value": it.get("parsed_value"), "why": it.get("why")}
            elif sup == "unclear":
                prov["confidence"] = min(prov.get("confidence", 1.0), 0.5)
                flagged[f] = {"verifier_value": it.get("parsed_value"), "why": it.get("why"), "unclear": True}
        r["_verify"] = {"model": VERIFIER, "checked": len(items), "flagged": len(flagged), "fields": flagged}
        n_flag += len(flagged)
        print(f"  {r['sim_id']}: {len(items)} numbers checked, {len(flagged)} flagged")
    write_jsonl(path, recs)
    print(f"done; {n_flag} fields flagged across {len(todo)} records")
