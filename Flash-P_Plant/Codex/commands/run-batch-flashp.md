# Codex Recipe: run-batch-flashp

Alias for `batch-run-flashp`.

Use when the user asks for `/run-batch-flashp`, `run-batch-flashp`, batch FLASH-P runs, running a phenotype list, `--skip-existing`, or interval/wave scheduling.

## Script

Run the Codex-native scheduler:

```bash
python Agent/shared/codex_batch_run_flashp.py <args>
```

This launches one `codex exec` process per phenotype. It does not call Claude.

## Examples

```bash
python Agent/shared/codex_batch_run_flashp.py --species "Sorghum bicolor" --phenotypes-file Phenotype.txt --max-parallel 2
python Agent/shared/codex_batch_run_flashp.py --species "Sorghum bicolor" --phenotypes-file Phenotype.txt --max-parallel 2 --skip-existing
python Agent/shared/codex_batch_run_flashp.py --species "Sorghum bicolor" --phenotypes-file Phenotype.txt --skip-existing --batch-size 4 --batch-interval-min 300 --max-parallel 2
python Agent/shared/codex_batch_run_flashp.py --targets-file targets.txt --dry-run
python Agent/shared/codex_batch_run_flashp.py --summarize-run-dir batch_runs/codex_flashp_batch_<timestamp>
```

## Rules

- Use `--max-parallel 2` by default unless the user asks otherwise.
- Use `--skip-existing` when rerunning an updated phenotype list; it skips targets only when the expected `networks/<PhenotypeSlug>` run has completed validation/export outputs. Partial failed folders are retried.
- Use `--batch-size <N> --batch-interval-min <minutes>` for quota/token reset waves.
- Use `--summarize-run-dir <batch_dir>` after a batch finishes or partially fails to write current `failed_*.txt` and `incomplete_*.txt` rerun files.
- Use `--dry-run` first if the request is ambiguous.
- Logs and manifest are written under `batch_runs/codex_flashp_batch_<timestamp>/`.
