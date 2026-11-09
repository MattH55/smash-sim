# Extraction prompt — simulation_record

You are extracting structured data about biomolecular **pulling simulations** from a scientific paper. Output must validate against `schema/simulation_record.schema.json`. Use the controlled vocabularies in `vocab/model_class.csv` and `vocab/implementation.csv`.

## Rules

1. **One record per distinct (molecule, model class, protocol) combination.** A paper that pulls I27 with all-atom SMD at three velocities is ONE record with `pulling_velocity_range_m_s`; a paper that pulls I27 and ubiquitin is TWO records; a paper that pulls I27 with SMD and with a Gō model is TWO records.
2. **Report, don't infer numbers.** If a value is not in the paper (main text, methods, SI, figure captions), set it to `null`. Never estimate a velocity, spring constant, or force. The only derivations allowed are unit conversions (Å/ps → m/s; kcal/mol/Å² → pN/nm; nm/ns → m/s), and each conversion must be noted in `_prov` with the original quote.
3. **Unit conversions:** 1 Å/ps = 100 m/s; 1 nm/ns = 1 m/s; 1 kcal mol⁻¹ Å⁻² = 695 pN/nm; 1 kJ mol⁻¹ nm⁻² = 1.66 pN/nm; 1 kBT at 298 K = 4.11 pN·nm. Loading rate = spring constant × velocity only if the authors state the effective loading rate or the spring is stiff; otherwise leave `loading_rate_pN_s` null.
4. **class_id** must be one of the vocabulary IDs. Decide by the energy function, not by the software: a Gō model run in GROMACS is `CG-GO`, not `AA-EXP`.
5. **event_types_probed**: tag A-types only when the paper analyses stretching of the unstructured/homogeneous chain itself (persistence length, overstretching plateau, elasticity); tag B-types by what breaks. `B-formation` only when refolding/rebinding is simulated, not merely discussed.
6. **topology_tag**: `series` if contacts break sequentially each bearing full load (hairpin unzipping, zipper); `parallel` if several contacts share load and rupture cooperatively (shear duplex, β-strand clamps like I27 A′–G); `cross` for pseudoknots / overlapping contact sets; `homopolymer` for A-only records; `ligand-pocket` for receptor–ligand unbinding.
7. **benchmark**: fill `hardware`, `wall_time_per_trajectory_h`, `throughput_ns_per_day`, `core_hours_total` only if stated. If `system_size` and `trajectory_length_ns` and `throughput_ns_per_day` allow a derived wall time, you may fill it and set `benchmark_source: "derived"`. Otherwise `"absent"`.
8. **observables**: take the primary reported value at the primary velocity. If several velocities, record the slowest (closest to experiment) and say so in `*_note`.
9. **pathway_summary**: an ordered arrow-list of structural elements in the order they are lost, using the authors' element names.
10. **experimental_comparison_in_paper**: `present: true` only if the authors compare to a specific SMFS measurement (cite a DOI or give the experimental number). Comparison to melting or bulk kinetics does not count. Record the experimental DOIs you can identify.
11. **_prov**: for every non-null numeric field and for `class_id`, `protocol.mode`, `molecule.name`, give the supporting quote (≤300 chars), page/section, and a confidence 0–1. Fields without provenance will be rejected.
12. If the paper is purely analytical (no trajectories), still produce a record with `class_id` in {AN-*, KIN-*}, `impl_id: "NONE"`, `sampling` all null.

## Output

Return ONLY a JSON array of records. No prose. Leave `sim_id` as `"SIM-000000"`; the pipeline assigns IDs.

## Input

<paper doi="{{doi}}">
{{full_text}}
</paper>
