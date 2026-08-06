#!/usr/bin/env python3
"""
Launch multiple FLASH-P runs from a phenotype list.

This helper starts one Claude Code run per phenotype and can keep a small local
process pool so multiple traits run in parallel without requiring many open
terminal windows.

Examples:
  python Agent/shared/batch_run_flashp.py --species "Sorghum bicolor" --phenotypes-file phenotypes.txt --max-parallel 2 --skip-existing
  python Agent/shared/batch_run_flashp.py --species "Arabidopsis thaliana" "Shoot Branching" "Seed Size" --dry-run
  python Agent/shared/batch_run_flashp.py --targets-file targets.txt --mode background
  python Agent/shared/batch_run_flashp.py --species "Sorghum bicolor" --phenotypes-file phenotypes.txt --batch-size 4 --batch-interval-min 30
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence


@dataclass
class Job:
    index: int
    phenotype: str
    target: str
    name: str
    log_path: Path
    command: List[str]
    process: Optional[subprocess.Popen] = None
    returncode: Optional[int] = None


@dataclass
class SkippedTarget:
    phenotype: str
    target: str
    reason: str
    network_dir: Path


def slugify(value: str, max_len: int = 64) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", value.strip()).strip("_")
    slug = slug or "flashp"
    return slug[:max_len]


def split_inline_items(values: Sequence[str]) -> List[str]:
    items: List[str] = []
    for value in values:
        for part in re.split(r"[;\n]", value):
            part = part.strip()
            if part:
                items.append(part)
    return items


def read_lines(path: Path) -> List[str]:
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Allow CSV-ish first column, but keep spaces inside phenotype names.
        if "," in line:
            line = line.split(",", 1)[0].strip()
        if line:
            lines.append(line)
    return lines


def normalize_target(entry: str, species: Optional[str]) -> tuple[str, str]:
    entry = entry.strip().strip('"')
    match = re.search(r"\s+in\s+", entry, flags=re.IGNORECASE)
    if match:
        phenotype = entry[: match.start()].strip()
        return phenotype, entry
    if not species:
        raise SystemExit(
            f"Phenotype {entry!r} has no species. Provide --species or use '<phenotype> in <species>'."
        )
    return entry, f"{entry} in {species}"


def build_prompt(target: str) -> str:
    return f"""Execute one full FLASH-P Light run for this target:

{target}

Read `.claude/commands/run-flashp.md`, substitute `$ARGUMENTS` with `{target}`, and follow that
command exactly. Run the full pipeline to completion. Do not ask the user for clarification unless
the target is impossible to parse. Keep source/network outputs isolated to the target's own network
folder. In the final response, report the network directory, best method, accuracy, failures, and
export status concisely.
"""


def build_claude_command(
    target: str,
    name: str,
    mode: str,
    model: str,
    permission_mode: str,
) -> List[str]:
    prompt = build_prompt(target)
    cmd = ["claude"]
    if mode == "background":
        cmd.append("--bg")
    else:
        cmd.append("-p")
        cmd.extend(["--output-format", "text"])
    cmd.extend(["--name", name])
    if model:
        cmd.extend(["--model", model])
    if permission_mode:
        cmd.extend(["--permission-mode", permission_mode])
    cmd.append(prompt)
    return cmd


def collect_targets(args: argparse.Namespace) -> List[tuple[str, str]]:
    entries: List[str] = []
    if args.phenotypes_file:
        entries.extend(read_lines(Path(args.phenotypes_file)))
    if args.targets_file:
        entries.extend(read_lines(Path(args.targets_file)))
    entries.extend(split_inline_items(args.phenotypes))

    if not entries:
        raise SystemExit("No phenotypes supplied. Use --phenotypes-file, --targets-file, or positional names.")

    targets: List[tuple[str, str]] = []
    seen = set()
    for entry in entries:
        phenotype, target = normalize_target(entry, args.species)
        key = target.lower()
        if key in seen:
            continue
        seen.add(key)
        targets.append((phenotype, target))
    return targets


def network_dir_for_phenotype(cwd: Path, phenotype: str) -> Path:
    return cwd / "networks" / slugify(phenotype)


def filter_existing_targets(
    args: argparse.Namespace,
    cwd: Path,
    targets: List[tuple[str, str]],
) -> tuple[List[tuple[str, str]], List[SkippedTarget]]:
    if not args.skip_existing:
        return targets, []

    kept: List[tuple[str, str]] = []
    skipped: List[SkippedTarget] = []
    for phenotype, target in targets:
        network_dir = network_dir_for_phenotype(cwd, phenotype)
        if network_dir.exists():
            skipped.append(
                SkippedTarget(
                    phenotype=phenotype,
                    target=target,
                    reason="network directory already exists",
                    network_dir=network_dir,
                )
            )
        else:
            kept.append((phenotype, target))
    return kept, skipped


def make_jobs(args: argparse.Namespace, run_dir: Path, targets: List[tuple[str, str]]) -> List[Job]:
    logs_dir = run_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    jobs: List[Job] = []
    for index, (phenotype, target) in enumerate(targets, start=1):
        slug = slugify(phenotype)
        name = f"flashp-{slug}"
        log_path = logs_dir / f"{index:03d}_{slug}.log"
        command = build_claude_command(target, name, args.mode, args.model, args.permission_mode)
        jobs.append(Job(index=index, phenotype=phenotype, target=target, name=name, log_path=log_path, command=command))
    return jobs


def write_manifest(
    run_dir: Path,
    args: argparse.Namespace,
    jobs: Sequence[Job],
    skipped: Sequence[SkippedTarget],
) -> None:
    manifest = {
        "created": dt.datetime.now(dt.timezone.utc).isoformat(),
        "cwd": str(Path.cwd()),
        "mode": args.mode,
        "model": args.model,
        "permission_mode": args.permission_mode,
        "max_parallel": args.max_parallel,
        "skip_existing": args.skip_existing,
        "batch_size": args.batch_size,
        "batch_interval_min": args.batch_interval_min,
        "skipped": [
            {
                "phenotype": item.phenotype,
                "target": item.target,
                "reason": item.reason,
                "network_dir": str(item.network_dir),
            }
            for item in skipped
        ],
        "jobs": [
            {
                "index": job.index,
                "phenotype": job.phenotype,
                "target": job.target,
                "name": job.name,
                "log_path": str(job.log_path),
                "command": job.command[:-1] + ["<prompt>"],
            }
            for job in jobs
        ],
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def print_plan(
    jobs: Sequence[Job],
    skipped: Sequence[SkippedTarget],
    run_dir: Path,
    args: argparse.Namespace,
) -> None:
    print(f"Batch directory: {run_dir}")
    print(f"Mode: {args.mode}")
    print(f"Max parallel: {args.max_parallel}")
    if args.batch_size:
        print(f"Batch size: {args.batch_size}")
        print(f"Batch interval minutes: {args.batch_interval_min:g}")
    print(f"Skip existing: {args.skip_existing}")
    print(f"Skipped: {len(skipped)}")
    for item in skipped:
        print(f"  SKIP {item.target} ({item.reason}: {item.network_dir})")
    print(f"Jobs: {len(jobs)}")
    for job in jobs:
        print(f"  {job.index:>2}. {job.target} -> {job.log_path}")


def launch_background(jobs: Sequence[Job], cwd: Path, dry_run: bool) -> int:
    print("Dispatching Claude-managed background agents.")
    if dry_run:
        for job in jobs:
            print("DRY RUN:", " ".join(job.command[:-1]), "<prompt>")
        return 0

    failed = 0
    for job in jobs:
        print(f"Starting {job.name}: {job.target}")
        result = subprocess.run(
            job.command,
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        job.log_path.write_text(result.stdout or "", encoding="utf-8")
        if result.returncode != 0:
            failed += 1
            print(f"  FAILED to dispatch: see {job.log_path}")
        else:
            print(f"  dispatched; dispatch log: {job.log_path}")
        time.sleep(1)
    return 1 if failed else 0


def launch_local_pool(jobs: List[Job], cwd: Path, max_parallel: int, dry_run: bool) -> int:
    if dry_run:
        for job in jobs:
            print("DRY RUN:", " ".join(job.command[:-1]), "<prompt>")
        return 0

    pending = list(jobs)
    running: List[Job] = []
    finished: List[Job] = []

    def start(job: Job) -> None:
        log_file = job.log_path.open("w", encoding="utf-8")
        log_file.write(f"FLASH-P batch job {job.index}: {job.target}\n")
        log_file.write(f"Started: {dt.datetime.now().isoformat()}\n\n")
        log_file.flush()
        job.process = subprocess.Popen(
            job.command,
            cwd=str(cwd),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )
        # Keep handle so it is not garbage collected before process exit.
        job.process._flashp_log_file = log_file  # type: ignore[attr-defined]
        print(f"Started {job.name} pid={job.process.pid} log={job.log_path}")

    try:
        while pending or running:
            while pending and len(running) < max_parallel:
                job = pending.pop(0)
                start(job)
                running.append(job)

            still_running: List[Job] = []
            for job in running:
                assert job.process is not None
                rc = job.process.poll()
                if rc is None:
                    still_running.append(job)
                    continue
                job.returncode = rc
                log_file = getattr(job.process, "_flashp_log_file", None)
                if log_file:
                    log_file.write(f"\nFinished: {dt.datetime.now().isoformat()} returncode={rc}\n")
                    log_file.close()
                finished.append(job)
                status = "OK" if rc == 0 else f"FAILED rc={rc}"
                print(f"Finished {job.name}: {status}")
            running = still_running
            if pending or running:
                time.sleep(10)
    except KeyboardInterrupt:
        print("Interrupted. Terminating running Claude jobs...")
        for job in running:
            if job.process and job.process.poll() is None:
                job.process.terminate()
        raise

    failed = [job for job in finished if job.returncode != 0]
    print("\nBatch complete.")
    print(f"Successful: {len(finished) - len(failed)}")
    print(f"Failed: {len(failed)}")
    if failed:
        for job in failed:
            print(f"  {job.name}: {job.log_path}")
    return 1 if failed else 0


def launch_local_batches(
    jobs: List[Job],
    cwd: Path,
    max_parallel: int,
    dry_run: bool,
    batch_size: int,
    batch_interval_min: float,
) -> int:
    if batch_size < 1:
        raise SystemExit("--batch-size must be >= 1 when supplied.")
    batches = [jobs[i : i + batch_size] for i in range(0, len(jobs), batch_size)]
    failed = 0
    total_batches = len(batches)
    interval_seconds = max(batch_interval_min, 0.0) * 60.0

    for batch_index, batch in enumerate(batches, start=1):
        print(f"\nStarting batch {batch_index}/{total_batches} ({len(batch)} jobs).")
        rc = launch_local_pool(list(batch), cwd, max_parallel, dry_run)
        failed += int(rc != 0)
        if batch_index < total_batches and interval_seconds > 0:
            print(f"Waiting {batch_interval_min:g} minutes before next batch...")
            time.sleep(interval_seconds)
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run multiple /run-flashp targets from a phenotype list.")
    parser.add_argument("phenotypes", nargs="*", help="Phenotype names, or full '<phenotype> in <species>' targets.")
    parser.add_argument("--species", help="Species name to append to phenotype-only entries.")
    parser.add_argument("--phenotypes-file", help="Text file with one phenotype per line.")
    parser.add_argument("--targets-file", help="Text file with one '<phenotype> in <species>' target per line.")
    parser.add_argument("--max-parallel", type=int, default=2, help="Maximum local Claude processes at once.")
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip phenotypes whose expected networks/<PhenotypeSlug> directory already exists.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        help="Run jobs in waves of this many targets. Useful with --batch-interval-min.",
    )
    parser.add_argument(
        "--batch-interval-min",
        type=float,
        default=0.0,
        help="Minutes to wait between --batch-size waves.",
    )
    parser.add_argument(
        "--mode",
        choices=("local", "background"),
        default="local",
        help="local waits and manages a process pool; background dispatches Claude-managed agents.",
    )
    parser.add_argument("--model", default="opus", help="Claude model alias for each run.")
    parser.add_argument(
        "--permission-mode",
        default="acceptEdits",
        help="Claude Code permission mode for each run, e.g. acceptEdits, auto, bypassPermissions.",
    )
    parser.add_argument("--run-dir", help="Directory for manifest/logs. Defaults to batch_runs/<timestamp>.")
    parser.add_argument("--dry-run", action="store_true", help="Print planned commands without launching Claude.")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.max_parallel < 1:
        raise SystemExit("--max-parallel must be >= 1")
    if args.batch_size is not None and args.batch_size < 1:
        raise SystemExit("--batch-size must be >= 1")
    if args.batch_interval_min < 0:
        raise SystemExit("--batch-interval-min must be >= 0")
    if args.mode == "background" and args.batch_size:
        raise SystemExit("--batch-size/--batch-interval-min are supported in local mode only.")

    cwd = Path.cwd().resolve()
    if not (cwd / "CLAUDE.md").exists() or not (cwd / "Agent").exists():
        raise SystemExit("Run this from the Flash-P Claude folder, e.g. Flash-P_Plant/Claude.")

    targets = collect_targets(args)
    targets, skipped = filter_existing_targets(args, cwd, targets)
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = Path(args.run_dir) if args.run_dir else cwd / "batch_runs" / f"flashp_batch_{timestamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    jobs = make_jobs(args, run_dir, targets)
    write_manifest(run_dir, args, jobs, skipped)
    print_plan(jobs, skipped, run_dir, args)

    if not jobs:
        print("No jobs to run.")
        return 0

    if args.mode == "background":
        return launch_background(jobs, cwd, args.dry_run)
    if args.batch_size:
        return launch_local_batches(
            jobs,
            cwd,
            args.max_parallel,
            args.dry_run,
            args.batch_size,
            args.batch_interval_min,
        )
    return launch_local_pool(jobs, cwd, args.max_parallel, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
