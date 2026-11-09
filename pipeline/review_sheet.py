#!/usr/bin/env python3
"""Step 7 — export a flat human-check sheet and re-import corrections.

export: data/review/sim_records_review.csv with the fields most prone to extraction error
        (velocity, loading rate, spring constant, n_trajectories, class_id, topology, match level, verdict)
        plus the supporting quote and confidence for each, so a curator can verify in one glance.
import: read the edited CSV back, apply changes, set curation.status = human_checked.

Run:  python review_sheet.py export
      python review_sheet.py import data/review/sim_records_review.csv --curator "MH"
"""
from __future__ import annotations

import argparse
import datetime as dt

import pandas as pd

from common import load_config, read_jsonl, write_jsonl

cfg = load_config()
R = cfg["_data"] / "review"
R.mkdir(exist_ok=True)

CHECK_FIELDS = ["class_id", "impl_id", "molecule.name", "molecule.pdb_id", "molecule.topology_tag",
                "protocol.mode", "protocol.pulling_velocity_m_s", "protocol.loading_rate_pN_s",
                "protocol.spring_constant_pN_nm", "sampling.n_trajectories",
                "observables.delta_Lc_nm", "observables.rupture_force_pN", "benchmark.hardware",
                "benchmark.wall_time_per_trajectory_h"]


def get(d, path):
    for p in path.split("."):
        d = (d or {}).get(p)
    return d


def set_(d, path, val):
    parts = path.split(".")
    for p in parts[:-1]:
        d = d.setdefault(p, {})
    d[parts[-1]] = val


def export():
    rows = []
    for r in read_jsonl(cfg["_data"] / "40_records.jsonl"):
        row = {"sim_id": r["sim_id"], "doi": r["doi"], "status": r.get("curation", {}).get("status")}
        for f in CHECK_FIELDS:
            row[f] = get(r, f)
            p = r.get("_prov", {}).get(f, {})
            row[f + "__quote"] = p.get("quote")
            row[f + "__conf"] = p.get("confidence")
        row["curator_note"] = ""
        rows.append(row)
    df = pd.DataFrame(rows)
    # low-confidence first
    conf_cols = [c for c in df.columns if c.endswith("__conf")]
    df["min_conf"] = df[conf_cols].min(axis=1)
    df.sort_values("min_conf").to_csv(R / "sim_records_review.csv", index=False)
    print(f"exported {len(df)} records -> {R/'sim_records_review.csv'} (sorted by lowest confidence)")


def import_(path, curator):
    df = pd.read_csv(path)
    recs = read_jsonl(cfg["_data"] / "40_records.jsonl")
    by_id = {r["sim_id"]: r for r in recs}
    changed = 0
    for _, row in df.iterrows():
        r = by_id.get(row["sim_id"])
        if not r:
            continue
        touched = False
        for f in CHECK_FIELDS:
            new = row[f]
            if pd.isna(new):
                new = None
            old = get(r, f)
            if (old is None and new is None) or (old is not None and new is not None and str(old) == str(new)):
                continue
            try:
                new = type(old)(new) if old is not None and not isinstance(old, bool) else new
            except Exception:
                pass
            set_(r, f, new)
            touched = True
        r["curation"] = {"status": "human_checked", "curator": curator, "date": dt.date.today().isoformat(),
                         "notes": None if pd.isna(row.get("curator_note")) else row.get("curator_note")}
        changed += touched
    write_jsonl(cfg["_data"] / "40_records.jsonl", recs)
    print(f"imported; {changed} records had field changes; all marked human_checked by {curator}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["export", "import"])
    ap.add_argument("path", nargs="?")
    ap.add_argument("--curator", default="unknown")
    a = ap.parse_args()
    export() if a.action == "export" else import_(a.path, a.curator)
