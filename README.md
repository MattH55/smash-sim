# smash-sim

Enrichment and concordance layer for **SMASH-SIM**, the pulling-simulation category of [SMASH-BIO](https://smash-bio.org). It consumes SMASH-BIO's published simulation records, fills in what regex extraction cannot (model class, protocol numbers, compute benchmarks, pathways, assumptions, load-sharing topology), and scores concordance with SMFS experiments **per observable**. The derived tables are the data behind the review *Biomolecular Pulling Simulation Models: Tradeoffs and Developments*.

It runs entirely on GitHub Actions; nothing needs a local machine.

## Why this exists

SMASH-BIO's `hub/sim/` pipeline harvests, classifies and builds `SIM-######` records with regex and gazetteers. As of the current release (123 records): 78 have model family `other`, 33 have a pulling velocity, 16 a spring constant, 0 have compute cost, and the match table links molecules, not observables. This repo adds:

| Layer | What | Where |
|---|---|---|
| Skeleton crosswalk | SMASH-BIO record → analysis schema, same `SIM-######` id, identifiers locked | `pipeline/ingest_smash.py` |
| LLM enrichment | fills nulls from full text with quotes; never overwrites verified identifiers | `pipeline/enrich.py`, `prompts/enrich_simulation_record.md` |
| Gap search | PubMed / Europe PMC / arXiv / **OpenAlex** for pulling-simulation papers SMASH-BIO has not harvested, plus OpenAlex citation snowballing from the seed cases and from every enriched record → provisional `SIMX-######` records and a candidate list for upstream. Runs as its own workflow (`gap-search.yml`) in parallel with enrichment | `pipeline/gap_search.py`, `screen.py`, `seed.py` |
| Comparisons | one row per (simulation, experiment, observable) with match level H2H / EXT / QUAL / PRED / UNTESTED, verdict, criterion, disagreement reason, and which triangle arrow it instantiates | `pipeline/compare.py`, `schema/comparison.schema.json` |
| Views | the review's tables, computed | `pipeline/views.py` → `data/views/` |
| Cross-model verification | a second model (Kimi K2.6) re-checks every extracted number against its quote; disagreements lower confidence and surface in the review sheet | `pipeline/verify.py` |
| Abstract tier | paywalled papers without a PDF still get a record from title + abstract, flagged `text_source: abstract`, confidence capped at 0.5 | `fetch_fulltext.py` (`abstract_fallback`) |
| Human check | lowest-confidence fields first, with verifier flags and coercions shown, re-importable | `pipeline/review_sheet.py` |

## Schema in one paragraph

`simulation_record` (`schema/simulation_record.schema.json`): identity · `class_id` from a 17-class controlled vocabulary (`vocab/model_class.csv`: energy function, dynamics, ions, native-structure dependence, event types covered) · `impl_id` (`vocab/implementation.csv`) · molecule with PDB/UniProt/sequence hash and a **load-sharing `topology_tag`** (series / parallel / cross / homopolymer / ligand-pocket) · **event types probed** (A-entropic, A-enthalpic, A-cooperative stretching; B-single, B-series, B-parallel, B-cross, B-formation bond events) · protocol (mode, velocity, loading rate, spring, ions, Mg²⁺, solvent) · sampling · **benchmark** (system size, hardware, wall time, throughput, core/GPU hours; reported / derived / absent) · observables (ΔLc, F, Δx‡, k₀, ΔG‡, PMF, intermediates, pathway, refolding, hysteresis, Lp, plateau force) · reduction model(s) · stated assumptions · in-paper experimental comparison · per-field provenance quotes.

`comparison` (`schema/comparison.schema.json`): `sim_id` × `smfs_id` × `observable` → `match_level`, `rate_overlap_log10`, `extrapolation_model`, sim/exp values, `verdict` + `verdict_criterion`, `disagreement_reasons` (controlled), `direction` (sim_predicts_exp · exp_validates_sim · exp_parameterizes_model · exp_falsifies_model · model_reduces_sim · sim_validates_model · mutual), `comparison_made_by`, `source_doi`. Conditional rules: EXT requires the extrapolation model and the rate gap; partial/disagree require a reason.

## Running it

### On GitHub Actions (intended)

1. Repo **Settings → Secrets and variables → Actions** → add `NANOGPT_API_KEY` (default provider; key from nano-gpt.com/api, format `sk-nano-…`). To use Anthropic directly instead, add `ANTHROPIC_API_KEY` and set `llm.provider: anthropic` in `pipeline/config.yaml`. `NCBI_API_KEY` is optional.
2. **Actions → SMASH-SIM pipeline → Run workflow**. Defaults: `ingest,fulltext,enrich,verify,compare,views,review_export`, limit 0 (= all). Use a small limit for a dry run.
   **Actions → SMASH-SIM gap search → Run workflow** expands the corpus beyond SMASH-BIO; it runs in parallel and commits `data/20_screened.jsonl`, which the next pipeline run then fetches, enriches (as `SIMX-` records) and compares.
3. Results are committed back to `data/` and the views are attached to the run as an artifact.
4. A weekly scheduled run re-ingests SMASH-BIO and enriches anything new (idempotent: already-enriched ids are skipped).

To run the gap search: steps `gap_search,seed,screen,fulltext,enrich,compare,views`. The screened candidate list (`data/20_screened.jsonl`) is also what to hand upstream to SMASH-BIO's harvester.

### Locally

```bash
pip install -r requirements.txt
export NANOGPT_API_KEY=sk-nano-...
python pipeline/run.py --steps ingest,fulltext,enrich,compare,views --limit 10
```

Paywalled papers: drop PDFs into `data/pdfs/<doi with / → _>.pdf` (see `data/30_missing.csv`) and rerun `fulltext`.

## Scaling to the full literature

The gap search (first pass) identified 18,163 records, 16,606 after deduplication, 4,087 passing keyword rules and 2,305 passing the LLM screen: 1,008 pulling-simulation papers, 365 explicit simulation–experiment comparisons, 408 analytical-model papers and 523 experiment-only SMFS papers. The experiment-only set is written to `data/20_smfs_candidates_for_smash_bio.jsonl` as a hand-back for SMASH-BIO's harvester. The other 1,781 are enriched in priority order (comparisons → simulations → analytical; full text before abstract) in chunks of 150 records per scheduled run (every 6 h), so the full corpus completes in a few days without exceeding Actions' job limit. `data/prisma_counts.json` holds the PRISMA numbers.

## Data flow

```
SMASH-BIO downloads ──ingest──▶ 10_skeletons.jsonl ─┐
PubMed/EuropePMC/arXiv ──gap_search──▶ 20_hits ──screen──▶ 20_screened.jsonl ─┤
                                                     fulltext ◀──────────────┘
                                                        │ 30_fulltext_index.jsonl
                                                     enrich  (LLM)  ──▶ 40_records.jsonl  (+40_rejected)
                                                        │
SMASH-BIO sim_matches.csv + smfs_records ─────▶ compare (LLM) ──▶ 50_pairs.jsonl, 50_comparisons.jsonl
                                                        │
                                                     views ──▶ data/views/*.csv ; review_sheet ──▶ data/review/
```

## Views → article

- `per_class.csv` — systems explored, velocity / spring / system-size / wall-time envelopes, fraction with refolding and pathway: **tradeoffs table**.
- `per_observable.csv` — observable × match level × verdict: **concordance table** ("ΔLc robust, kinetics fragile", computed).
- `arrows.csv` — direction × verdict: **how the three sources supported one another**.
- `rate_gap_hist.csv` — log₁₀(sim/exp loading rate) for H2H/EXT rows: **velocity-gap figure**.
- `disagreement_reasons_by_class.csv` — **caveats, quantified**.
- `class_x_event_grid.csv`, `gaps_covered_but_unstudied.csv`, `untested_predictions.csv` — **open challenges**.
- `prisma_counts.json` — PRISMA flow numbers for the gap search.

## Extraction rules (enforced in code and prompt)

- Numbers are never inferred; every non-null numeric field needs a quote in `_prov` or the record is rejected.
- Unit conversions are allowed and logged: Å/ps ×100 → m/s; kcal mol⁻¹ Å⁻² ×695 → pN/nm; kJ mol⁻¹ nm⁻² ×1.66 → pN/nm.
- Class is decided by the energy function, not the software.
- SMASH-BIO identifiers (`sim_id`, `doi`, PDB, UniProt/Rfam) are restored after enrichment even if the model changed them.
- Benchmarks store only reported values; wall time is derived only from reported throughput × length and flagged.
- Comparison candidates come from SMASH-BIO's match table and from DOIs the simulation paper cites; gap records may also match on PDB/UniProt identity. Nothing matches on name alone.

## Handing results back to SMASH-BIO

`data/40_records.jsonl` keeps SMASH-BIO ids, so enrichment can be merged upstream field-by-field (SMASH-BIO's `curation.status` would become `manually_reviewed` for rows marked `human_checked` here). `data/50_comparisons.jsonl` is new: it replaces the single `match_level` per molecule pair with one verdict per observable and is what the SMASH-SIM explorer should display.

## Seed set

`seed/seed_cases.csv` lists 30 anchor simulation–experiment cases from the literature review (biotin–streptavidin, titin I27, GFP, talin, vWF A2, P5GA/P5abc, oxDNA overstretching, add riboswitch, telomeric G4, α3D …) with their expected match level and verdict. They serve as the regression set: after a run, their comparison rows should reproduce these verdicts.
