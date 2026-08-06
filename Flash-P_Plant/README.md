# FLASH-P Light

A token-lean version of FLASH-P, built to produce a full validated network in **one Claude Pro
session**. Same science as the full pipeline (identical validation results) — but the JSON is
slimmed ~80–97%, the judges run a single pass, and **Literature Review is knowledge-first +
WebSearch only — no full-text WebFetch and no subagents** (the two things that blow the token
budget).

The three folders are identical except the orchestrator filename:
- **Claude Code** → `Claude/` (`CLAUDE.md`)
- **Codex** → `Codex/` (`AGENTS.md`)
- **OpenCode / Aider / Goose** → `OpenCode_Aider_Any_Other/` (`AGENTS.md`)

---

## Codex commands

Codex command-style workflows live in **`Codex/commands/`**. Open Codex in `Codex/` and ask for
`/run-flashp`, `/batch-run-flashp`, `/simulation`, `/merge-flashp`, `/run-flashp-studio`,
`/run-flashp-epistasis`, `/run-flashp-gxe`, `/run-flashp-compare`, or `/run-flashp-help`; `AGENTS.md`
maps those names to the matching recipe. The Codex batch scheduler is
`Codex/Agent/shared/codex_batch_run_flashp.py` and launches `codex exec` sessions.

## How to run

1. Open your tool in the matching folder above.
2. `/clear` to start a clean session (so the token count reflects just this run).
3. Paste this prompt, filling in your trait + species:

   > **Run the full FLASH-P pipeline for Shoot Branching in Arabidopsis. Single agent only — do NOT launch
   > subagents and do NOT WebFetch full papers. Knowledge-first draft, then WebSearch to verify each
   > edge/test and take the DOI from the search result.**

It runs Steps 1→6 and writes the network, validation, and supplementary tables under a new
`{trait}_network/` folder.

---

## Measure the token usage

- **During / right after the run:** type `/cost` — shows this session's token totals.
- **Detailed, afterwards (terminal):** `npx ccusage@latest` — reads Claude Code's local logs and
  reports input / output / cache tokens per session.

Compare the total against your plan's limits. On **Pro**, the whole run should fit one usage window.

---

## One big token-saving tip

Right after Step 1 finishes, run:

```
python Agent/shared/compact.py --network {trait}_network
```

This converts `curated_edges.json` (the file the Builder + Judge re-read the most) to TOON —
about **97% smaller** — *before* those steps read it. Optional, but it's the single biggest saving.

---

## What "Light" trades off

- Best for **well-studied** traits (the model knows the canonical biology); niche traits get more
  `literature_gap` flags.
- Every edge/test still carries a **real DOI taken from a WebSearch result** — never invented.
- Judges run **one pass** (vs up to 3) — slightly less thorough, still an independent biology check.
- Files use short keys / TOON — see `Agent/shared/LEXICON.md` for the legend.

## If it drifts

If the agent starts WebFetching whole papers or launching subagents, stop it and remind it:
**"knowledge-first + WebSearch only, single agent, no WebFetch."** (The prompts say this, but models
occasionally drift on long runs.)
