# Enrichment prompt — complete a SMASH-BIO skeleton record

You are given a **skeleton** record for a biomolecular pulling simulation, produced automatically from a published paper by regex extraction (SMASH-BIO), plus the paper's full text. Complete it so that it validates against `schema/simulation_record.schema.json`, using the controlled vocabularies in `vocab/model_class.csv` and `vocab/implementation.csv`.

## What to keep, what to fill, what to fix

- **Keep as-is (authoritative):** `sim_id`, `doi`, `molecule.pdb_id`, `molecule.uniprot_or_rfam`, `_smashbio`. These identifiers were resolver-verified.
- **Fill every null you can support with a quote.** The skeleton is typically missing: `class_id` (when `UNRESOLVED`), `impl_id`, velocity, loading rate, spring constant, trajectory count and length, ions/Mg²⁺, solvent, `benchmark.*`, all of `observables`, `pathway_summary`, `event_types_probed`, `molecule.topology_tag`, `reduction_model`, `stated_assumptions`, and `experimental_comparison_in_paper`.
- **Fix a skeleton value only if the paper plainly contradicts it**, and when you do, put the correcting quote in `_prov` for that field and mention the correction in `curation.notes`.
- **Never estimate numbers.** Null is correct when the paper does not give the value. Unit conversions only: 1 Å/ps = 100 m/s; 1 nm/ns = 1 m/s; 1 kcal mol⁻¹ Å⁻² = 695 pN/nm; 1 kJ mol⁻¹ nm⁻² = 1.66 pN/nm; 1 kBT(298 K) = 4.11 pN·nm; record the original expression in the quote.

## Decision rules

1. **class_id by energy function, not software.** Gō in GROMACS → `CG-GO`. SMD with CHARMM/AMBER/OPLS in explicit water → `AA-EXP`. GB/EEF1 → `AA-IMP`. Umbrella/metadynamics/Jarzynski ensembles along extension → `AA-ENH`. SOP/SOP-SC → `CG-SOP`. WSME/binary-variable → `CG-ISING`. oxDNA/oxRNA → `CG-OXDNA`. TIS → `CG-TIS`. Martini/GōMartini → `CG-MARTINI`. Secondary-structure kinetic models → `KIN-STEM`; NN unzipping → `KIN-UNZIP`; purely analytic → `AN-*`. If the paper uses two classes, return two records (second with the same `sim_id`; the pipeline suffixes it).
2. **event_types_probed:** A-types only if the paper analyses stretching of the chain itself (persistence length, enthalpic modulus, overstretching/unstacking plateau). B-types by what breaks: `B-single` (one bond/ligand), `B-series` (zipper, sequential), `B-parallel` (shear, load-sharing clamp), `B-cross` (pseudoknot / overlapping contacts), `B-formation` (refolding or rebinding actually simulated).
3. **topology_tag:** dominant load-sharing geometry of the rupture event (`series`, `parallel`, `cross`, `homopolymer`, `ligand-pocket`, `mixed`); `unknown` if the paper does not allow a call.
4. **benchmark:** only reported values; `hardware_class` from the hardware string; `benchmark_source` = `reported` if any cost field is filled, else `absent`. Do not derive.
5. **observables:** primary value at the slowest reported velocity; say which velocity in `*_note`. `pathway_summary` is an ordered `->` list of the authors' structural element names.
6. **reduction_model:** every analytical model the authors apply to their simulation output (Bell–Evans fit, DHS, Jarzynski, WLC for ΔLc…). `none` if raw trajectories only.
7. **stated_assumptions:** assumptions the authors state or that the class implies (`native_centric` and `no_nonnative` for Gō/SOP; `no_explicit_ions` for most CG; `pathway_rate_invariant` whenever they compare fast-pull mechanism to slow experiment; `one_d_reaction_coordinate` whenever Bell/DHS is applied; `fixed_persistence_length` whenever a WLC with a quoted Lp is used).
8. **experimental_comparison_in_paper:** `present: true` only for a comparison to a specific SMFS measurement (DOI or quoted experimental number); list the experimental DOIs. Keep `smfs_ids` from the skeleton.
9. **_prov:** quote (≤300 chars), page/section, confidence 0–1 for every non-null numeric field and for `class_id`, `protocol.mode`, `molecule.name`, `molecule.topology_tag`. Keep existing `_prov` entries from the skeleton unless you corrected the field.

## Output

JSON array of one or more complete records. No prose.

## Inputs

<skeleton>
{{skeleton_json}}
</skeleton>

<paper doi="{{doi}}">
{{full_text}}
</paper>
