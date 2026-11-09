#!/usr/bin/env python3
"""Step 6 — derived views that become the review's tables and the SMASH-SIM explorer data.

Outputs (data/views/):
  per_class.csv            systems explored, velocity envelope, benchmark envelope, observables ever reproduced
  per_observable.csv       concordance by observable x match level  (the "dLc robust, kinetics fragile" table)
  arrows.csv               count of comparison rows per triangle arrow (direction) x verdict
  gaps.csv                 class x event-type cells with zero records; UNTESTED predictions list
  rate_gap_hist.csv        distribution of log10(sim/exp loading rate) for H2H/EXT rows
Run:  python 06_views.py
"""
from __future__ import annotations

import pandas as pd

from common import load_config, load_vocab, read_jsonl

cfg = load_config()
V = cfg["_data"] / "views"
V.mkdir(exist_ok=True)

recs = read_jsonl(cfg["_data"] / "40_records.jsonl")
source = "enriched records (40_records.jsonl)"
if not recs:
    recs = read_jsonl(cfg["_data"] / "10_skeletons.jsonl")
    source = "SKELETONS ONLY (no enrichment yet): per_class.csv reflects regex-level coverage"
sims = pd.json_normalize(recs)
cmps = pd.json_normalize(read_jsonl(cfg["_data"] / "50_comparisons.jsonl"))
classes = load_vocab("model_class")
classes.setdefault("UNRESOLVED", {"name": "unresolved (skeleton)", "event_types_covered": ""})
print("views from:", source)

if sims.empty:
    print("no records at all; nothing to do")
    raise SystemExit(0)

# --- per class -------------------------------------------------------------
def envelope(s):
    s = pd.to_numeric(s, errors="coerce").dropna()
    return f"{s.min():.2g}–{s.max():.2g}" if len(s) else ""

obs_cols = [c for c in sims.columns if c.startswith("observables.") and sims[c].dtype != object]
g = sims.groupby("class_id")
per_class = pd.DataFrame({
    "n_records": g.size(),
    "n_papers": g["doi"].nunique(),
    "n_molecules": g["molecule.name"].nunique(),
    "molecule_types": g["molecule.type"].agg(lambda x: ";".join(sorted(x.dropna().unique()))),
    "topologies": g["molecule.topology_tag"].agg(lambda x: ";".join(sorted(x.dropna().unique()))),
    "velocity_m_s_envelope": g["protocol.pulling_velocity_m_s"].agg(envelope),
    "loading_rate_pN_s_envelope": g["protocol.loading_rate_pN_s"].agg(envelope),
    "spring_pN_nm_envelope": g["protocol.spring_constant_pN_nm"].agg(envelope),
    "system_size_envelope": g["benchmark.system_size"].agg(envelope),
    "wall_h_per_traj_envelope": g["benchmark.wall_time_per_trajectory_h"].agg(envelope),
    "n_with_benchmark": g["benchmark.benchmark_source"].agg(lambda x: (x != "absent").sum()),
    "frac_refolding": g["observables.refolding_simulated"].mean().round(2),
    "frac_pathway": g["observables.pathway_described"].mean().round(2),
})
per_class.insert(0, "class_name", [classes.get(c, {}).get("name", "") for c in per_class.index])
per_class.to_csv(V / "per_class.csv")

# --- per observable concordance -------------------------------------------
if not cmps.empty:
    po = pd.crosstab([cmps["observable"]], [cmps["match_level"], cmps["verdict"]])
    po["n"] = po.sum(axis=1)
    agree_cols = [c for c in po.columns if isinstance(c, tuple) and c[1] == "agree"]
    po["frac_agree"] = (po[agree_cols].sum(axis=1) / po["n"]).round(2)
    h2h_cols = [c for c in po.columns if isinstance(c, tuple) and c[0] == "H2H"]
    po["n_H2H"] = po[h2h_cols].sum(axis=1)
    po.sort_values("n", ascending=False).to_csv(V / "per_observable.csv")

    pd.crosstab(cmps["direction"], cmps["verdict"]).to_csv(V / "arrows.csv")

    rg = cmps.dropna(subset=["rate_overlap_log10"])
    pd.cut(rg["rate_overlap_log10"], bins=[-1, 0.5, 1, 2, 3, 5, 7, 9, 12]).value_counts().sort_index() \
        .rename("n").to_csv(V / "rate_gap_hist.csv")

    reasons = cmps.explode("disagreement_reasons").dropna(subset=["disagreement_reasons"])
    pd.crosstab(reasons["disagreement_reasons"], reasons["sim_id"].map(sims.set_index("sim_id")["class_id"])) \
        .to_csv(V / "disagreement_reasons_by_class.csv")

# --- gaps ------------------------------------------------------------------
ev = sims.explode("event_types_probed").dropna(subset=["event_types_probed"])
grid = pd.crosstab(ev["class_id"], ev["event_types_probed"])
all_ev = ["A-entropic", "A-enthalpic", "A-cooperative", "B-single", "B-series", "B-parallel", "B-cross", "B-formation"]
grid = grid.reindex(index=list(classes), columns=all_ev, fill_value=0)
grid.to_csv(V / "class_x_event_grid.csv")
gaps = [(c, e) for c in grid.index for e in grid.columns if grid.loc[c, e] == 0
        and e in classes[c]["event_types_covered"].split(";")]
pd.DataFrame(gaps, columns=["class_id", "event_type"]).to_csv(V / "gaps_covered_but_unstudied.csv", index=False)

if not cmps.empty:
    ut = cmps[cmps["match_level"] == "UNTESTED"].merge(sims[["sim_id", "doi", "molecule.name", "class_id"]], on="sim_id", how="left")
    cols = [c for c in ["sim_id", "doi", "molecule.name", "class_id", "observable", "verdict_criterion", "note"] if c in ut.columns]
    ut[cols].to_csv(V / "untested_predictions.csv", index=False)

print("views written to", V)
for f in sorted(V.glob("*.csv")):
    print("  ", f.name)
