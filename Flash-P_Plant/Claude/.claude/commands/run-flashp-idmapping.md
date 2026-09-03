---
description: Map the gene nodes of an existing FLASH-P trait network to stable gene model identifiers — offline name cache, ortholog projection and pooled gene databases, judged node by node. Writes an annotated copy; the original network.json is never touched.
argument-hint: <network dir>  e.g. "networks/Dhurrin_Content_In_Sorghum"
model: claude-sonnet-4-6
---

# FLASH-P gene identifier mapping

Target network directory: **$ARGUMENTS**  (the `<NET>` for this run; resolve a bare trait name to `networks/<Trait>/`).

You are orchestrating **gene identifier mapping** on an **already-built** FLASH-P network (this command
does NOT build a network — use `/run-flashp` for that). The heavy, deterministic work lives in
`Agent/shared/idmap/prepare.py`; the judgement lives in the **`flashp-gene-id-mapper`** subagent. Your
job is to run the preparation, dispatch the subagent, build the report, and relay its findings —
especially the **limitations** — back to the user. Keep it token-lean: pipe script output through
`tail`, read only the printed summary, and never dump `node_dossiers.json` or the full TSVs into the
thread.

## Why this needs judgement and not a lookup (already baked into the pipeline)
- **Crop annotations name very few genes.** Sorghum carries a symbol for 1.2% of its genes, wheat 1.5%,
  tomato 4.4%; Arabidopsis manages 37%. A cache miss for a crop is close to uninformative.
- **Names are borrowed.** 43% of gene nodes have supporting literature drawn *exclusively* from another
  species — a sorghum node called `PIF4` is an Arabidopsis symbol, and querying sorghum for it cannot
  succeed. The pipeline resolves the name in its own species, then projects.
- **No single route is reliable**, so candidates are scored by how many *independent* routes agree:
  offline name cache, identifiers stated in the node's own cited papers, pooled Ensembl/NCBI/UniProt,
  and ortholog projection corroborated across Ensembl Compara, Gramene and PLAZA.
- **"Unresolved" is often the correct answer.** A pipeline that always returns an identifier is
  returning wrong ones, and the downstream cost is a CRISPR guide against the wrong locus.

## What the scripts write (already baked in) → `<NET>/idmapping/`
`node_dossiers.json` (the working file), `judgements.jsonl` (the subagent's decisions), `mapping.tsv`
(19 columns, one row per mappable node), `unresolved.tsv`, `network.idmapped.json` (the annotated
**copy**), `anchor_agreement.tsv`, `report.html`.

- `relation` ∈ `one2one` · `homoeolog_set` · `family_set` · `complex_members` · `proxy` · `ambiguous` · `unresolved`
- `confidence` ∈ `high` (two or more independent routes agreeing — nothing else earns it) · `medium` · `low` · `none`

## Execution plan

1. **Resolve & sanity-check `<NET>`.** Confirm `<NET>/network/network.json` (or flat `<NET>/network.json`)
   exists. If not, tell the user the path isn't a FLASH-P network and stop. Also check for
   `<NET>/data/evidence.json` — without it this network predates Step 1.6 and the literature routes are
   inert (see exit code 3 below).

2. **Prepare the dossiers:**
   ```
   python Agent/shared/idmap/prepare.py <NET>
   ```
   This makes network calls and takes roughly 5–8 minutes for a 35-node network, so run it in the
   background and poll. It prints a readiness summary and any routing warnings. Forward user hints as
   flags: `--offline` (cache only, no network), `--no-plaza` (skip a large one-off download for an
   unseen species pair), `--workers N`, `--limit N`, `--allow-no-evidence`.

   Exit codes: **2** = not a FLASH-P network directory. **3** = no `data/evidence.json`; offer either
   `python Agent/shared/idmap/backfill_evidence.py --network <NET> --out <NET>/idmapping/backfilled`
   or re-running with `--allow-no-evidence`, and say that confidence drops accordingly. **4** = no
   mappable gene nodes. Relay the message and stop in each case.

3. **Dispatch the `flashp-gene-id-mapper` subagent** on `<NET>`. It reads the dossiers, adjudicates the
   identifiers cited papers name, writes `judgements.jsonl`, and runs `emit_mapping.py` itself. It runs
   on **opus** and keeps the whole dossier in its own context — that is why it is a subagent. Wait for
   it to return (~20 lines).

   If `emit_mapping.py` rejects a judgement it exits **5**; that is the subagent's to fix. Never
   suggest `--allow-partial` to get past it.

4. **Build the report:**
   ```
   python Agent/shared/idmap/build_report.py --outdir <NET>/idmapping
   ```

5. **Relay the findings.** Concisely, from the subagent's summary and the printed counts:
   - how many gene nodes there were and how many got an identifier — **always paired with the species'
     own symbol coverage**, because 30% on a species with 475 named genes is not the same result as
     30% on Arabidopsis;
   - the breakdown by **relation** and by **confidence**;
   - **all routing warnings verbatim** — most importantly a species with no Ensembl assembly (answers
     come back as `relation: proxy` in a relative, which is a different claim) or a cache whose "names"
     are a second identifier system rather than symbols;
   - the nodes the subagent found hardest, and why;
   - point the user at `<NET>/idmapping/report.html` for the full table.

## Guard rails
- Do not modify `network/`, `data/`, equations, or validation results — identifier mapping is
  **read-only** with respect to the network; it only writes under `<NET>/idmapping/`. The annotated
  network is a **copy** (`network.idmapped.json`): FLASH-P's schema drops keys it does not know on the
  next round-trip, so a `gid` written into `network/network.json` would be silently lost.
- Do not resolve a gene name yourself, and do not hand-pick an identifier out of a candidate set —
  that is the subagent's job, and `emit_mapping.py` enforces the rules it works under.
- Only identifiers a route actually produced may be emitted. An untraceable identifier is worse than
  none; the honest answer is `unresolved`.
- Do not report a coverage figure without the species' cache coverage beside it, and do not present
  `unresolved` as a failure — for a QTL interval or a process node it is the correct answer.
- No accuracy figure is claimed for this pipeline. Do not invent one.
