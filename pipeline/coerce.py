"""Schema-driven coercion of LLM output: map free text in enum fields to the nearest allowed value.

Walks the JSON Schema; for every enum-typed property whose value is not allowed:
  1. case-insensitive exact match
  2. keyword rules (below) for the fields where models most often paraphrase
  3. substring match of an allowed value inside the text
  4. fallback: 'other' / 'unknown' / None if allowed, else drop the value
The original text is kept in rec["_coerced"][field_path]. Also drops unknown top-level keys and unknown items in enum arrays.
"""
from __future__ import annotations
import re

KEYWORDS = {
    "protocol.solvent": [("implicit", "implicit"), ("explicit", "explicit"), ("vacuum|no solvent|none", "none")],
    "protocol.ions_treatment": [("debye|screened|dh\\b", "debye_huckel"), ("explicit", "explicit"), ("implicit|effective|generalized born|gb\\b", "implicit_other"), ("no ion|none|neutral", "none")],
    "protocol.pull_coordinate": [("c.?alpha|cα|ca atoms?|termin", "termini_CA"), ("phosph|\\bP atoms?|3'|5'", "termini_P"), ("cent(er|re) of mass|\\bcom\\b", "COM_distance"), ("ligand|pocket|binding site", "ligand_pocket"), ("residue|atom|bead|nucleotide", "specified_atoms")],
    "protocol.mode": [("constant.?velocity|constant.?speed|cv-?smd|steered", "constant_velocity"), ("constant.?force|force.?clamp|cf-?smd", "constant_force"), ("ramp|loading rate", "force_ramp"), ("quench|refold", "force_quench"), ("umbrella", "umbrella"), ("metadynamics", "metadynamics"), ("replica", "replica_exchange"), ("jarzynski|crooks|work", "jarzynski_pulls"), ("equilib", "equilibrium_bias")],
    "benchmark.hardware_class": [("a100|h100|v100|a40|a6000|tesla|datacenter|data center", "gpu_datacenter"), ("rtx|gtx|titan|geforce|consumer", "gpu_consumer"), ("cluster|supercomputer|hpc|nodes", "cluster"), ("gpu|cuda", "gpu_consumer"), ("cpu|core", "cpu")],
    "benchmark.system_size_unit": [("atom", "atoms"), ("bead", "beads"), ("nucleotide|\\bnt\\b", "nucleotides"), ("residue", "residues")],
    "molecule.topology_tag": [("pseudoknot|cross|overlap", "cross"), ("shear|parallel|clamp", "parallel"), ("zipper|series|sequential|unzip", "series"), ("ligand|pocket|unbind", "ligand-pocket"), ("homopolymer|dsdna|ssdna|elastic", "homopolymer"), ("mixed", "mixed")],
    "molecule.type": [("protein.?protein|dimer|complex", "protein-protein"), ("ligand|small molecule|biotin", "protein-ligand"), ("protein.?(dna|rna|nucleic)|(dna|rna).?protein", "protein-NA"), ("\\brna\\b", "RNA"), ("\\bdna\\b", "DNA"), ("protein|peptide|domain", "protein"), ("polysacch|sugar|dextran", "polysaccharide")],
    "benchmark.benchmark_source": [("report|stated|given", "reported"), ("deriv|comput|estimat", "derived"), ("absent|none|not", "absent")],
}
EVENT_KEYWORDS = [("entropic|wlc|fjc|persistence", "A-entropic"), ("enthalpic|modulus|tension|stretch", "A-enthalpic"), ("overstretch|plateau|unstack|cooperative|b-s|melting", "A-cooperative"),
                  ("refold|rebind|formation|fold", "B-formation"), ("pseudoknot|cross", "B-cross"), ("shear|parallel|clamp", "B-parallel"), ("zipper|series|sequential|unzip|hairpin", "B-series"), ("single|ligand|unbind|bond", "B-single")]


def _enum_fix(path, value, allowed, log):
    if value in allowed:
        return value
    if value is None and None in allowed:
        return None
    text = str(value)
    low = text.lower().strip()
    for a in allowed:
        if a is not None and str(a).lower() == low:
            log[path] = text; return a
    for pat, target in KEYWORDS.get(path, []):
        if re.search(pat, low) and target in allowed:
            log[path] = text; return target
    subs = [a for a in allowed if a is not None and len(str(a)) > 2 and str(a).lower().replace("_", " ") in low.replace("_", " ").replace("-", " ")]
    if subs:
        best = max(subs, key=lambda a: len(str(a)))   # prefer the most specific allowed value (bell_evans over bell)
        log[path] = text; return best
    for fb in ("other", "unknown", None):
        if fb in allowed:
            log[path] = text; return fb
    log[path] = text
    return None


IMPL_ALIASES = {"namd3": "NAMD", "namd2": "NAMD", "namd": "NAMD", "openmm": "OPENMM", "gromacs": "GROMACS", "amber": "AMBER",
                "smog": "SMOG2", "smog2": "SMOG2", "oxdna": "OXDNA", "oxrna": "OXDNA", "lammps": "LAMMPS", "plumed": "PLUMED",
                "charmm": "CHARMM", "cafemol": "CAFEMOL", "sop-gpu": "SOP-GPU", "sop": "SOP", "martini": "MARTINI", "tis": "TIS",
                "kinfold": "KINFOLD", "viennarna": "KINFOLD", "in-house": "INHOUSE", "inhouse": "INHOUSE", "custom": "INHOUSE", "none": "NONE"}


def fix_impl(rec, allowed, log):
    v = rec.get("impl_id")
    if v is None or v in allowed:
        return
    low = str(v).lower().strip()
    hit = IMPL_ALIASES.get(low) or next((t for k, t in IMPL_ALIASES.items() if k in low), None)
    log["impl_id"] = str(v)
    rec["impl_id"] = hit if hit in allowed else "INHOUSE"


def flatten_value(x, log, path):
    """{'value': 400, 'unit': 'pN', 'note': ...} -> 400 (unit/note returned for the caller)."""
    if isinstance(x, dict):
        log[path] = str(x)[:200]
        return x.get("value"), x.get("unit"), x.get("note")
    return x, None, None


def coerce(rec: dict, schema: dict) -> dict:
    log = rec.setdefault("_coerced", {})
    # comparison rows: dict-valued sim/exp values
    for fld in ("sim_value", "exp_value"):
        if isinstance(rec.get(fld), dict):
            val, unit, note = flatten_value(rec[fld], log, fld)
            rec[fld] = val
            if unit and not rec.get("units"):
                rec["units"] = unit
            if note:
                rec["note"] = ((rec.get("note") or "") + f" [{fld}: {note}]").strip()
    props = schema.get("properties", {})
    for k in list(rec.keys()):
        if k not in props and not k.startswith("_"):
            log[f"dropped:{k}"] = str(rec.pop(k))[:200]
    _walk(rec, props, "", log)
    # event types: array of enum
    ev = rec.get("event_types_probed")
    if isinstance(ev, list):
        allowed = props["event_types_probed"]["items"]["enum"]
        out = []
        for e in ev:
            if e in allowed:
                out.append(e); continue
            low = str(e).lower()
            hit = next((t for pat, t in EVENT_KEYWORDS if re.search(pat, low)), None)
            log[f"event_types_probed:{e}"] = hit or "dropped"
            if hit and hit not in out:
                out.append(hit)
        rec["event_types_probed"] = out
    for arr_field in ("reduction_model", "stated_assumptions"):
        v = rec.get(arr_field)
        if isinstance(v, list):
            allowed = props[arr_field]["items"]["enum"]
            kept = []
            for e in v:
                fixed = _enum_fix(f"{arr_field}[]", e, allowed, log)
                if fixed is not None and fixed not in kept:
                    kept.append(fixed)
            rec[arr_field] = kept
    for k, p in (rec.get("_prov") or {}).items():
        if isinstance(p, dict) and isinstance(p.get("quote"), str) and len(p["quote"]) > 300:
            p["quote"] = p["quote"][:299] + "…"
    if not log:
        rec.pop("_coerced", None)
    return rec


def _walk(obj, props, prefix, log):
    for key, spec in props.items():
        if key not in obj or key.startswith("_"):
            continue
        path = f"{prefix}{key}"
        val = obj[key]
        if "enum" in spec and not isinstance(val, list):
            obj[key] = _enum_fix(path, val, spec["enum"], log)
        elif isinstance(val, str) and spec.get("maxLength") and len(val) > spec["maxLength"]:
            log[path] = "truncated from %d chars" % len(val); obj[key] = val[: spec["maxLength"] - 1] + "…"
        elif spec.get("type") == "object" and "properties" in spec and isinstance(val, dict):
            for k in list(val.keys()):
                if k not in spec["properties"] and spec.get("additionalProperties") is False:
                    log[f"dropped:{path}.{k}"] = str(val.pop(k))[:200]
            _walk(val, spec["properties"], path + ".", log)
        elif spec.get("type") in ("number", ["number", "null"]) or (isinstance(spec.get("type"), list) and "number" in spec["type"]):
            if isinstance(val, str):
                m = re.search(r"-?\d+(\.\d+)?([eE][-+]?\d+)?", val.replace(",", ""))
                if m:
                    log[path] = val; obj[key] = float(m.group(0))
                else:
                    log[path] = val; obj[key] = None
        elif spec.get("type") in ("integer", ["integer", "null"]) or (isinstance(spec.get("type"), list) and "integer" in spec["type"]):
            if isinstance(val, str):
                words = {"one":1,"two":2,"three":3,"four":4,"five":5,"six":6,"seven":7,"eight":8,"nine":9,"ten":10,"twelve":12,"twenty":20}
                m = re.search(r"\d+", val.replace(",", ""))
                log[path] = val; obj[key] = int(m.group(0)) if m else words.get(val.strip().lower())
            elif isinstance(val, float):
                obj[key] = int(round(val))
        elif spec.get("type") == "boolean" and not isinstance(val, bool):
            log[path] = str(val); obj[key] = str(val).lower() in ("true", "yes", "1")
