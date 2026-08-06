---
description: Alias for /batch-run-flashp; launch multiple FLASH-P phenotype builds from a list, with controlled parallelism.
argument-hint: --species <species> --phenotypes-file <file> [--max-parallel 2] [--skip-existing]
model: opus
---

# FLASH-P batch run alias

Batch request: **$ARGUMENTS**

This is an alias for `/batch-run-flashp`. Use the same helper and execution rules:

```bash
python Agent/shared/batch_run_flashp.py <args>
```

Common examples:

```text
/run-batch-flashp --species "Sorghum bicolor" --phenotypes-file Phenotype.txt --max-parallel 2 --skip-existing
/run-batch-flashp --species "Sorghum bicolor" --phenotypes-file Phenotype.txt --skip-existing --batch-size 4 --batch-interval-min 300 --max-parallel 2
```

Begin by running `batch_run_flashp.py` with the parsed arguments. If uncertain, run it with `--dry-run`
and report the planned jobs.
