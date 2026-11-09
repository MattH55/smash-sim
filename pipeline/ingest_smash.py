#!/usr/bin/env python3
"""Step 1 — ingest SMASH-BIO's published SMASH-SIM and SMFS downloads and crosswalk them into the analysis schema.

Downloads (raw GitHub, so this runs anywhere GitHub is reachable):
  website/downloads/sim/smash-sim-records.jsonl.gz   -> data/smash/sim_records.jsonl.gz
  website/downloads/sim/smash-sim-matches.csv        -> data/smash/sim_matches.csv
  website/downloads/smash-smfs-records.jsonl.gz      -> data/smash/smfs_records.jsonl.gz

Crosswalk: each SMASH-BIO SIM record becomes a *skeleton* analysis record (data/10_skeletons.jsonl) with the same
SIM-###### id, class_id mapped from model.family, protocol numbers carried over, and `_prov` entries pointing at the
SMASH-BIO evidence. The LLM enrichment step then fills what regex extraction left empty and never overwrites a
verified identifier.

Run:  python ingest_smash.py [--ref main]
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import json

from common import append_jsonl, http_get, load_config, norm_doi, write_jsonl

cfg = load_config()
RAW = "https://raw.githubusercontent.com/{repo}/{ref}/website/downloads/"
FILES = {
    "sim/smash-sim-records.jsonl.gz": "sim_records.jsonl.gz",
    "sim/smash-sim-matches.csv": "sim_matches.csv",
    "smash-smfs-records.jsonl.gz": "smfs_records.jsonl.gz",
}

FAMILY_TO_CLASS = {
    "all_atom_explicit_solvent": "AA-EXP",
    "all_atom_implicit_solvent": "AA-IMP",
    "Go_CA": "CG-GO",
    "Go_heavy_atom": "CG-GO",
    "SOP": "CG-SOP",
    "AWSEM": "CG-GO",
    "Martini": "CG-MARTINI",
    "oxDNA": "CG-OXDNA",
    "oxRNA": "CG-OXDNA",
    "TIS": "CG-TIS",
    "HiRE_RNA": "CG-HIRE",
    "thermodynamic_nearest_neighbour": "KIN-UNZIP",
    "other": None,  # left for the LLM to decide
}
MODE_MAP = {
    "constant_velocity": "constant_velocity", "constant_force": "constant_force", "force_ramp": "force_ramp",
    "boxed_MD": "other", "equilibrium_under_force": "equilibrium_bias", "other": "other",
}
MOLTYPE_MAP = {"protein": "protein", "DNA": "DNA", "RNA": "RNA", "DNA_protein": "protein-NA",
               "RNA_protein": "protein-NA", "complex": "protein-protein"}
SOFTWARE_TO_IMPL = {"gromacs": "GROMACS", "namd": "NAMD", "amber": "AMBER", "openmm": "OPENMM", "charmm": "CHARMM",
                    "lammps": "LAMMPS", "smog": "SMOG2", "oxdna": "OXDNA", "cafemol": "CAFEMOL", "plumed": "PLUMED",
                    "sop-gpu": "SOP-GPU", "martini": "MARTINI"}

# SMASH-BIO result quantity -> analysis observable field (unit expected)
RESULT_MAP = {
    "unfolding_force": ("rupture_force_pN", "pN"), "rupture_force": ("rupture_force_pN", "pN"),
    "unbinding_force": ("rupture_force_pN", "pN"), "unzipping_force": ("rupture_force_pN", "pN"),
    "overstretching_force": ("plateau_force_pN", "pN"), "equilibrium_force": (None, None),
    "contour_length_increment": ("delta_Lc_nm", "nm"), "contour_length": ("delta_Lc_nm", "nm"),
    "distance_to_transition_state": ("dx_dagger_nm", "nm"), "rate_constant_zero_force": ("k0_s", "1/s"),
    "activation_free_energy": ("dG_dagger_kT", "kT"), "folding_free_energy": ("dG_unfold_kT", "kT"),
    "persistence_length": ("persistence_length_nm", "nm"), "number_of_steps_or_intermediates": ("n_intermediates", None),
}
TO_SI = {  # crude unit normalisation for the few units SMASH-BIO records use
    ("pN", "pN"): 1.0, ("nN", "pN"): 1e3, ("nm", "nm"): 1.0, ("Å", "nm"): 0.1, ("A", "nm"): 0.1, ("angstrom", "nm"): 0.1,
    ("1/s", "1/s"): 1.0, ("s^-1", "1/s"): 1.0, ("kT", "kT"): 1.0, ("kBT", "kT"): 1.0, ("kcal/mol", "kT"): 1 / 0.593,
    ("kJ/mol", "kT"): 1 / 2.479,
}


def q(d, *path, default=None):
    for p in path:
        if not isinstance(d, dict):
            return default
        d = d.get(p)
    return default if d is None else d


def qty(x, want_unit=None):
    """SMASH-BIO Quantity {value, unit} -> number in want_unit, or None."""
    if not isinstance(x, dict) or x.get("value") is None:
        return None
    v, u = x["value"], (x.get("unit") or "").strip()
    if want_unit is None:
        return v
    f = TO_SI.get((u, want_unit))
    return v * f if f is not None else None


def prov_from(evidence_list, field, confidence=0.6):
    """Turn SMASH-BIO evidence entries for `field` into a _prov entry."""
    for e in evidence_list or []:
        if e.get("field") == field:
            conf = {"high": 0.9, "medium": 0.6, "low": 0.4}.get(e.get("confidence"), confidence)
            return {"quote": (e.get("snippet") or "")[:300], "page": e.get("section"), "confidence": conf}
    return None


def crosswalk(r: dict) -> dict:
    model, proto, system, src = r.get("model", {}), r.get("protocol", {}), r.get("system", {}), r.get("source", {})
    speed = proto.get("pulling_speed_m_per_s")
    if speed is None and proto.get("pulling_speed"):
        sp = proto["pulling_speed"]
        u = (sp.get("unit") or "").replace(" ", "")
        conv = {"nm/ns": 1.0, "Å/ps": 100.0, "A/ps": 100.0, "nm/ps": 1000.0, "m/s": 1.0, "Å/ns": 0.1, "nm/s": 1e-9, "µm/s": 1e-6, "um/s": 1e-6}
        speed = sp["value"] * conv[u] if sp.get("value") is not None and u in conv else None
    spring = proto.get("spring_constant")
    spring_pn = None
    if spring and spring.get("value") is not None:
        u = (spring.get("unit") or "").replace(" ", "")
        conv = {"pN/nm": 1.0, "kJ/mol/nm^2": 1.66, "kJmol^-1nm^-2": 1.66, "kJ/mol/nm2": 1.66, "kcal/mol/Å^2": 695.0,
                "kcal/mol/A^2": 695.0, "kcal/mol/Å2": 695.0, "N/m": 1000.0}
        spring_pn = spring["value"] * conv[u] if u in conv else None

    structures = system.get("structures") or []
    pdb = next((s.get("id") for s in structures if (s.get("scheme") or s.get("database") or "").lower() == "pdb"), None)
    ents = system.get("entities") or []
    uni = next((i.get("id") for e in ents for i in e.get("identifiers", []) if i.get("scheme") in ("uniprot", "rfam")), None)
    if not pdb:
        pdb = next((i.get("id") for e in ents for i in e.get("identifiers", []) if i.get("scheme") == "pdb"), None)

    sw = [s.get("name", "").lower() for s in model.get("software") or []]
    impl = next((SOFTWARE_TO_IMPL[k] for k in SOFTWARE_TO_IMPL for s in sw if k in s), None)

    obs = {"pmf_available": False, "pathway_described": False, "refolding_simulated": False,
           "hysteresis_reported": False, "velocity_dependence_reported": False}
    prov = {}
    for res in r.get("results") or []:
        tgt, unit = RESULT_MAP.get(res.get("quantity"), (None, None))
        if not tgt or obs.get(tgt) is not None:
            continue
        val = qty(res.get("value") if isinstance(res.get("value"), dict) else {"value": res.get("value"), "unit": res.get("unit")}, unit) \
            if unit else (res.get("value", {}).get("value") if isinstance(res.get("value"), dict) else res.get("value"))
        if val is None:
            continue
        obs[tgt] = int(val) if tgt == "n_intermediates" else float(val)
        ev = res.get("evidence") or {}
        prov[f"observables.{tgt}"] = {"quote": (ev.get("snippet") or f"SMASH-BIO result {res.get('quantity')}")[:300],
                                      "page": ev.get("section"), "confidence": {"high": 0.9, "medium": 0.6}.get(ev.get("confidence"), 0.4)}

    rec = {
        "sim_id": r["record_id"],
        "doi": norm_doi(src.get("doi")) or f"smashbio:{r['record_id']}",
        "year": src.get("year") or 0,
        "title": src.get("title"),
        "first_author": (src.get("authors") or [None])[0],
        "preprint": False,
        "class_id": FAMILY_TO_CLASS.get(model.get("family")) or "UNRESOLVED",
        "impl_id": impl,
        "impl_version": None,
        "force_field_or_params": model.get("force_field") or model.get("parameter_set"),
        "molecule": {
            "name": system.get("name") or "unknown",
            "type": MOLTYPE_MAP.get(system.get("molecule_type"), "other"),
            "pdb_id": pdb, "uniprot_or_rfam": uni,
            "sequence": system.get("sequence"), "sequence_sha1": None,
            "construct_boundaries": q(system, "construct", "residue_range") or q(system, "construct", "nucleotide_range"),
            "n_residues_or_nt": None, "oligomer_state": None,
            "linkers_modelled": False, "linker_description": None, "topology_tag": "unknown",
        },
        "event_types_probed": [],
        "protocol": {
            "mode": MODE_MAP.get(proto.get("mode"), "other"),
            "pulling_velocity_m_s": speed, "pulling_velocity_range_m_s": None,
            "loading_rate_pN_s": None, "loading_rate_range_pN_s": None,
            "spring_constant_pN_nm": spring_pn, "constant_force_pN": None,
            "pull_coordinate": None, "pull_geometry_note": q(proto, "geometry", "description"),
            "temperature_K": qty(proto.get("temperature")), "monovalent_salt_mM": None, "mg_mM": None,
            "ions_treatment": None,
            "solvent": model.get("solvent") if model.get("solvent") in ("explicit", "implicit", "none") else None,
        },
        "sampling": {"n_trajectories": proto.get("n_trajectories"),
                     "trajectory_length_ns": qty(proto.get("simulated_time_per_trajectory")),
                     "cg_time_units": None, "total_sampled_time_ns": None, "timestep_fs": None},
        "benchmark": {"system_size": None, "system_size_unit": None, "hardware": None, "hardware_class": None,
                      "wall_time_per_trajectory_h": None, "throughput_ns_per_day": None, "core_hours_total": None,
                      "gpu_hours_total": None, "benchmark_source": "absent"},
        "observables": obs,
        "reduction_model": [],
        "stated_assumptions": [],
        "experimental_comparison_in_paper": {"present": False, "same_molecule": None, "experiment_dois": [],
                                             "smfs_ids": [m.get("record_id") or m.get("id") for m in q(r, "links", "experimental_matches", default=[]) if isinstance(m, dict)],
                                             "note": None},
        "curation": {"status": "auto", "curator": "smash-bio-crosswalk", "date": None, "notes": f"crosswalk from SMASH-BIO schema {r.get('schema_version')}"},
        "_prov": prov,
        "_smashbio": {"family": model.get("family"), "schema_version": r.get("schema_version"),
                      "unit_system": model.get("unit_system"), "curation": r.get("curation")},
    }
    # carry SMASH-BIO evidence into _prov where fields were filled
    for fld, smash_fld in [("protocol.mode", "protocol.mode"), ("protocol.pulling_velocity_m_s", "protocol.pulling_speed"),
                           ("protocol.spring_constant_pN_nm", "protocol.spring_constant"), ("protocol.temperature_K", "protocol.temperature"),
                           ("sampling.n_trajectories", "protocol.n_trajectories"), ("molecule.name", "system.name")]:
        p = prov_from(proto.get("evidence"), smash_fld) or prov_from(system.get("evidence"), smash_fld)
        if p:
            rec["_prov"][fld] = p
    rec["_prov"].setdefault("class_id", {"quote": f"SMASH-BIO model.family={model.get('family')}", "page": None,
                                         "confidence": 0.9 if rec["class_id"] != "UNRESOLVED" else 0.1})
    rec["_prov"].setdefault("molecule.name", {"quote": system.get("name") or "", "page": None, "confidence": 0.7})
    rec["_prov"].setdefault("protocol.mode", {"quote": f"SMASH-BIO protocol.mode={proto.get('mode')}", "page": None, "confidence": 0.6})
    return rec


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default=cfg.get("smash_bio_ref", "main"))
    args = ap.parse_args()
    out = cfg["_data"] / "smash"
    out.mkdir(exist_ok=True)
    base = RAW.format(repo=cfg.get("smash_bio_repo", "MattH55/smash-bio"), ref=args.ref)
    for remote, local in FILES.items():
        data = http_get(base + remote).content
        (out / local).write_bytes(data)
        print(f"  {remote}: {len(data)/1e3:.0f} kB")

    skeletons = []
    with gzip.open(out / "sim_records.jsonl.gz", "rt") as f:
        for line in f:
            if line.strip():
                skeletons.append(crosswalk(json.loads(line)))
    write_jsonl(cfg["_data"] / "10_skeletons.jsonl", skeletons)
    unresolved = sum(s["class_id"] == "UNRESOLVED" for s in skeletons)
    with_speed = sum(s["protocol"]["pulling_velocity_m_s"] is not None for s in skeletons)
    print(f"{len(skeletons)} skeleton records; class unresolved: {unresolved}; with velocity: {with_speed}")
