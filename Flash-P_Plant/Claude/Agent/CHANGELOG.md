# Changelog

> Entries headed `## [vX.Y.Z]` below are **pipeline releases**, tagged in git
> (`git tag -l`, [GitHub Releases](https://github.com/CMits/Flash-P/releases)) — each
> generated network's `flash_p_version` metadata field is derived live from these tags
> (see `Agent/shared/flashp_version.py`). Entries headed `## [X.Y]` are the older, informal
> **AGENT-SPEC version** log (CURATOR/BUILDER/etc. individually versioned) that predates the
> git-tag release system — kept as historical detail, not renumbered.

## [v1.1.0] - 2026-08-05

### Added
- **Step 1.6 — EVIDENCE VERIFICATION** (new mandatory pipeline step): `verify_evidence.py`
  resolves every edge/test DOI against Europe PMC, PubMed and OpenAlex, requires a sentence in
  the paper naming both of the claim's entities, replaces DOIs that don't support their claim,
  and quarantines what it can't ground. Writes `data/evidence.json` + `data/fulltext/*.txt`.
- **Species/organism provenance**: each claim now records which organism its evidence actually
  came from (curated on perturbation tests, read back from the paper for edges), so a network
  built largely on evidence from another species reads as such instead of implying it throughout.
- **Studio Evidence tab**: browse every edge/perturbation claim for a network — DOI, verification
  status (verified / DOI repaired / unverified), species (flagged when foreign to the modelled
  species), and the verbatim supporting sentence highlighted in place in the source paper
  (abstract or full text).
- **Studio Evidence downloads**: JSON and CSV export of the full edges + perturbations database
  per network, straight from the Evidence tab. JSON is shaped to mirror `Flash-P_DataBase`'s
  `paper`/`edge`/`perturbation` tables for a future atlas upload.
- **Git-tag-derived versioning**: `flashp_version.py` computes `flash_p_version` live from
  `git describe`, and `cut_release.py` cuts a tagged release end-to-end — no more hand-typed
  version literals.
- **Atlas contribution export**, and provenance propagated to the Medical and Animal build variants.

### Changed
- **Step 7 (Studio build)** now asks before opening the Studio at the end of a full `/run-flashp`
  run and delivers the file directly, instead of a silent browser auto-launch that a tool
  invocation may not reliably surface.
- **Perturbation provenance**: the DOI now carries through into
  `reconciled_perturbation_dataset.json`, not just onto edges.

### Fixed
- Studio: network scan now embeds exactly one final network per trait (was also picking up
  refinement snapshots).
- Studio: fixed Layered/Hierarchy layout edge overlap; added node search in the View tab.
- Step 1.6: fixed a quota-stall that made verification take hours; stopped mangling gene names
  during grounding.

## [v1.0.0] - 2026-08-05

### Baseline release
Retroactively tagged snapshot of `main`, marking the point the git-tag release system was
introduced. Functionally identical to the untagged code that preceded it — see the AGENT-SPEC
entries below for what had actually shipped by this point.

## [1.1] - 2026-03-31

### Changed — Post Shoot-Branching Run Lessons Baked Into Specs

Lessons from the first complete run (Arabidopsis shoot branching, 59 nodes, 105 tests, 95% accuracy):

- **CLAUDE.md v1.1**: Added 4 CRITICAL SIGNAL PROPAGATION TRAPS section:
  1. Positive feedback loops between hormone and transporter (Auxin↔PIN1 trap)
  2. Redundant gene modifiers too low (use 0.99 for triple-redundant single KO)
  3. Signaling mutant rescue experiments (structural limitation of additive exogenous_supply)
  4. Dead-end nodes creating false unchanged predictions
- **CLAUDE.md v1.1**: Added rules 8-11: no disconnected nodes, comprehensive testing (100+ tests), curated_edges as full repository, authors in candidate_papers
- **CLAUDE.md v1.1**: Completely revised file structure showing data flow (CURATOR→BUILDER→PERTURBATION→VALIDATOR→REFINEMENT→EXPORT)
- **CLAUDE.md v1.1**: Added supplementary principle (S1-S2 = everything FOUND, S3-S7 = what was USED)
- **BUILDER v1.1**: Added 5 CRITICAL SIGNAL PROPAGATION TRAPS with examples and fixes. Added network size target (55-65 nodes). Expanded quality checklist to 15 items.
- **CURATOR v1.4**: Output files changed. curated_edges.json now requires `edge_id` and `in_model` fields. perturbation_dataset.json target: 100+ tests. candidate_papers.json MUST include `authors`. Added "Extract EVERYTHING" principle.
- **Key insight**: curated_edges.json is the FULL REPOSITORY of all literature edges. The BUILDER selects from it. This shows comprehensive curation → intelligent selection in supplementary tables.

## [1.3] - 2026-03-30

### Changed — Exhaustive Multi-Source Paper Discovery + Maximally Inclusive Networks

- **CURATOR v1.3**: Major expansion. Now requires 10+ WebSearch rounds across 3+ source strategies (PMC, Frontiers, PLoS, MDPI, bioRxiv, Nature OA, Oxford Academic OA, PubMed abstracts). Scale guidance: 60-120+ papers for well-studied phenotypes. Expected network sizes: 40-80+ nodes, 80-200+ edges. Gap-fill is MANDATORY — agent must check every hormone cascade for completeness. Cross-species searches added for conserved pathways.
- **VALIDATOR v1.3**: Supplementary tables + Cytoscape now MUST be generated from BEST model (post-refinement). Evidence carry-through rule: reconciled dataset MUST preserve all evidence from perturbation dataset. Added Step 5 to regenerate Cytoscape from refined_network.json.
- **CLAUDE.md v1.3**: Added 4 new core principles: multi-source discovery, full text from all OA sources, maximally inclusive networks, evidence carry-through, final model outputs. Version bumped to 1.3.
- **Evidence in supplementary**: Table_S1/S2/S3 now explicitly require doi, paper_title, evidence_sentence columns for publication citation.
- **Network completeness check**: After compilation, agent must verify node/edge count is within expected range — if too low, go back to Phase A and search for missing pathways.

## [1.2] - 2026-03-30

### Changed — Batched Literature Review for Exhaustive Coverage

- **CURATOR v1.2**: Complete rewrite of workflow. Now uses batched approach: Phase A (5+ WebSearch discovery rounds to build master paper list), Phase B (read papers in batches of 5-8 via subagents), Phase C (compile and gap-fill). Explicit year-range coverage requirement. Scale guidance: well-studied phenotypes should read 40-80+ papers.
- **PERTURBATION v1.2**: Same batched approach. Reuses CURATOR's papers, adds experiment-focused searches.
- **CLAUDE.md v1.2**: Added Core Principle #3 "Exhaustive reading" with scale guidance. Version bumped to 1.2.
- **Subagent parallelism**: Both CURATOR and PERTURBATION specs now explicitly recommend launching 2-3 Agent subagents per round to read paper batches in parallel.
- **Year-range coverage**: Papers must span 1999-2026, not cluster in one era. Agent must check for gaps and do targeted searches.
- **candidate_papers.json**: New output — master list of all papers found during discovery, before reading.

## [1.1.2] - 2026-03-30

### Changed — Systematic Literature Review Approach

- **CURATOR**: Complete rewrite. Now performs systematic literature review — PubMed search (1999-present), reads full papers via PMC WebFetch, extracts edges with exact evidence sentences. Produces `papers_read.json` log.
- **PERTURBATION**: Same systematic approach — reads papers to extract experiments. Reuses CURATOR's papers + searches for additional experiment-focused papers.
- **CLAUDE.md**: Core principle changed from "WebSearch-first" to "Systematic literature review with full text reading". Evidence levels: full_text_read > abstract_read > pubmed_crosschecked.
- **Evidence standard**: Exact sentences quoted from papers (not paraphrased). `full_text_read` and `pmc_id` fields added to evidence schema.
- **Multiple evidence per edge**: Key edges should have 2+ supporting papers.

## [1.1.1] - 2026-03-30

### Changed — Evidence Quality + Encoding Fixes (post first network run)

- **Evidence standard**: Paper title AND evidence sentence now MANDATORY in every edge/perturbation evidence entry
- **DOI cross-check**: `verify_doi_in_pubmed()` recommended to confirm DOI exists and title matches
- **CURATOR**: Added competing pathway documentation, dead-end node rules, WebSearch tips from experience
- **PERTURBATION**: Added rescue experiment encoding rules (biosynthesis vs signaling mutant), chemical inhibitor modeling (NPA→PIN1 KD), composite member redundancy rules
- **REFINEMENT**: Added common encoding fixes section with real examples from first run
- **CLAUDE.md**: Added Evidence Quality Standard section with full schema

## [1.1] - 2026-03-30

### Changed — WebSearch-First, Minimal Python

- **All agents**: WebSearch is the primary method for discovering edges, perturbations, and DOIs. No Python scripts needed for discovery.
- **Python reduced to 3 validators**: Only `flashp_validator.py`, `ode_validator.py`, `rwr_validator.py` are required during the pipeline. Plus `export_supplementary.py` for post-processing.
- **ENVIRONMENT node bug fixed**: All nodes (including ENVIRONMENT) are 1.0 at WT baseline. Exogenous supply is additive (default 0).
- **Supplementary export fixed**: Now includes ALL 3 method CSVs (Table_S7a algebraic, S7b ODE, S7c RWR).
- **Removed from required pipeline**: `check_grounding.py`, `network_filters.py`, `network_to_cytoscape.py`, `equation_executor.py` — agent handles these tasks directly.
- **`literature_retriever.py`**: Only `verify_doi_in_pubmed()` kept as optional backup.
- **No hard thresholds**: No minimum node/edge/test counts, no accuracy targets.
- **Agent specs condensed**: ~5200 lines → ~600 lines across all specs.

## [1.0] - 2026-03-20

Initial release with strict DOI enforcement.
