---
description: List the FLASH-P slash commands available in this folder and what each one does.
argument-hint: (no arguments)
model: claude-haiku-4-5
---

# FLASH-P commands

Show the user a concise, friendly reference of the FLASH-P slash commands available **in this
folder**. Do NOT run any pipeline — this command only prints help.

## How to build the list (token-lean)
1. List the command files in `.claude/commands/` (every `*.md` except this one,
   `run-flashp-help.md`).
2. For each, read **only the YAML frontmatter** (the first few lines between the `---` fences) to
   get its `description` and `argument-hint`. Do not read the rest of the file.
3. Derive each command name from its filename (e.g. `run-flashp-studio.md` → `/run-flashp-studio`).

## What to print
A short intro line, then a markdown table with one row per command:

| Command | What it does | Usage |
|---|---|---|
| `/<name>` | (its `description`, trimmed to one line) | `/<name> <argument-hint>` |

Order the rows: **`/run-flashp` first** (the full pipeline build), then any analysis commands present
(`/run-flashp-epistasis`, `/run-flashp-gxe`), then `/run-flashp-studio`, and finally `/run-flashp-help`
(this command) last. Only include commands that actually exist here.

Close with one line: *pipeline outputs are written to `networks/<trait>/`, and this list reflects
exactly the commands available in this folder.*

Keep the whole response compact — a glanceable overview, no extra commentary.
