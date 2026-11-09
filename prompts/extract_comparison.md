# Extraction prompt — comparison

You are given (a) a simulation_record JSON already extracted, (b) the paper text, and (c) zero or more matched SMASH-BIO experiment records (SMFS-######) for the same molecule. Produce `comparison` rows that validate against `schema/comparison.schema.json`.

## Rules

1. **One row per observable compared.** If the paper compares rupture force AND pathway order AND ΔLc to experiment, that is three rows.
2. **match_level** is decided by loading rates, not by the authors' tone:
   - `H2H`: simulation and experiment loading rates (or velocities) overlap, or differ by < 1 log10 unit. Fill `rate_overlap_log10`.
   - `EXT`: authors extrapolate via a named model (Bell–Evans, DHS, BSK, FND) to compare. Fill `extrapolation_model` and `rate_overlap_log10`.
   - `QUAL`: comparison is of order, shape, rank, or presence/absence only.
   - `PRED`: the simulation publication predates the experiment and the experiment cites it as a prediction tested. Use `direction: sim_predicts_exp`.
   - `UNTESTED`: the paper makes an explicit experimentally-testable prediction and no matching experiment exists in SMASH-BIO.
3. **verdict** uses the authors' own judgement where stated; otherwise apply: `agree` if within reported uncertainty or within 20 % (forces, ΔLc) / same order (pathways); `partial` if some elements agree; `disagree` otherwise. Always fill `verdict_criterion`.
4. **disagreement_reasons** (required for partial/disagree): use the authors' stated reason if given, else `unknown`. Do not invent causes.
5. **direction**: which arrow of the triangle this instantiates:
   - `sim_predicts_exp` — simulation came first and predicted
   - `exp_validates_sim` — experiment used to check a simulation result
   - `exp_parameterizes_model` — experimental numbers tuned a force field or model
   - `exp_falsifies_model` — experiment shows the model/assumption fails
   - `model_reduces_sim` — an analytical model is used only to reduce simulation output (then `smfs_id` may be null)
   - `sim_validates_model` — simulation on a known landscape tests an analytical estimator
   - `mutual` — iterative joint study
6. **smfs_id**: use the provided matched SMASH-BIO records; if none matches the compared experiment, leave `smfs_id` null and fill `exp_doi`. Match on molecule identity and construct, not on name similarity.
7. **comparison_made_by**: `original_authors` if the comparison is in the simulation paper; `later_paper` if the comparing paper is different from the sim record's DOI (then `source_doi` is the later paper).
8. **_prov**: quote + page + confidence for `sim_value`, `exp_value`, `match_level`, `verdict`.

## Output

JSON array of comparison rows only. Leave `cmp_id` as `"CMP-000000"`.

## Inputs

<simulation_record>
{{sim_record_json}}
</simulation_record>

<matched_experiments>
{{smfs_records_json}}
</matched_experiments>

<paper doi="{{doi}}">
{{full_text}}
</paper>
