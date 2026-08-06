---
description: Launch multiple FLASH-P phenotype builds from a list, with controlled parallelism.
argument-hint: --species <species> --phenotypes-file <file> [--max-parallel 2] [--skip-existing]
model: opus
---

# FLASH-P batch run

Batch request: **$ARGUMENTS**

You are launching multiple independent `/run-flashp` jobs for one or more phenotypes. Each phenotype
must run in its own Claude Code process/session. Do not try to combine multiple phenotypes into one
single-trait pipeline run.

Use the helper:

```bash
python Agent/shared/batch_run_flashp.py <args>
```

## Supported examples

```text
/batch-run-flashp --species "Sorghum bicolor" --phenotypes-file phenotypes.txt --max-parallel 2
/batch-run-flashp --species "Sorghum bicolor" --phenotypes-file Phenotype.txt --max-parallel 2 --skip-existing
/batch-run-flashp --species "Arabidopsis thaliana" "Shoot Branching" "Seed Size" "Plant Height"
/batch-run-flashp --targets-file targets.txt --max-parallel 3
/batch-run-flashp --species "Sorghum bicolor" "Days To Flowering; Plant Height; Heat Stress Response" --dry-run
/batch-run-flashp --species "Sorghum bicolor" --phenotypes-file Phenotype.txt --skip-existing --batch-size 4 --batch-interval-min 30
```

Phenotype list file format:

```text
Days To Flowering
Plant Height
Heat Stress Response
```

Targets file format:

```text
Days To Flowering in Sorghum bicolor
Plant Height in Sorghum bicolor
```

## Execution Rules

1. Parse `$ARGUMENTS`.
2. If the user provides phenotypes directly, pass them as positional arguments to
   `batch_run_flashp.py`.
3. If the user provides a natural-language list, create a small text file under
   `batch_runs/input_lists/` and pass it with `--phenotypes-file`.
4. Require a species unless every line is already a full `<phenotype> in <species>` target.
5. Use `--max-parallel 2` by default unless the user asks otherwise.
6. If the user asks to run only new phenotypes, pass `--skip-existing`. This skips any target whose
   expected `networks/<PhenotypeSlug>` directory already exists.
7. If the user asks for interval/looped batches, pass `--batch-size <N> --batch-interval-min <minutes>`.
   This is local scheduler mode only.
8. Use `--dry-run` first if the request is ambiguous or if the user asks to preview.
9. Otherwise launch the batch.

## Modes

Default mode is local scheduler mode:

```bash
python Agent/shared/batch_run_flashp.py --species "..." --phenotypes-file phenotypes.txt --max-parallel 2
```

This keeps at most N `claude -p` jobs running at once and writes logs under:

```text
batch_runs/flashp_batch_<timestamp>/logs/
```

Interval mode in local scheduler:

```bash
python Agent/shared/batch_run_flashp.py --species "..." --phenotypes-file Phenotype.txt --skip-existing --batch-size 4 --batch-interval-min 30 --max-parallel 2
```

This starts up to 4 target jobs per wave, runs them with the local `--max-parallel` limit, then waits
30 minutes before releasing the next wave.

Optional Claude-managed background mode:

```bash
python Agent/shared/batch_run_flashp.py --mode background --species "..." --phenotypes-file phenotypes.txt
```

Background mode dispatches agents and returns quickly; use local mode when you want strict local
concurrency and logs.

## Safety

- Keep `--max-parallel` modest: 2 or 3 is usually the right range.
- Use `--skip-existing` when rerunning from an updated phenotype file to avoid overwriting or duplicating
  completed network folders.
- Use interval mode instead of an external `/loop` command unless a loop command is explicitly available
  in the user's Claude setup.
- Each target should create its own `networks/<Trait>` folder.
- Root-level `Fig_Data/` may be temporarily stale during parallel runs. After all runs complete, use
  `/merge-flashp <species> --all`.
- Do not use `bypassPermissions` unless the user explicitly asks for fully unattended operation and
  understands the risk.

Begin by running `batch_run_flashp.py` with the parsed arguments. If uncertain, run it with `--dry-run`
and report the planned jobs.
