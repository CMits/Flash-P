#!/usr/bin/env python3
"""
Launch multiple FLASH-P Codex runs from a phenotype list.

This is the Codex counterpart to Claude's batch_run_flashp.py. It starts one
`codex exec` process per phenotype, keeps a small local process pool, supports
skip-existing reruns, and can release jobs in delayed waves so new sessions start
after quota/token windows reset.

Examples:
  python Agent/shared/codex_batch_run_flashp.py --species "Sorghum bicolor" --phenotypes-file Phenotype.txt --max-parallel 2 --skip-existing
  python Agent/shared/codex_batch_run_flashp.py --species "Sorghum bicolor" --phenotypes-file Phenotype.txt --skip-existing --batch-size 4 --batch-interval-min 300
  python Agent/shared/codex_batch_run_flashp.py --targets-file targets.txt --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence


@dataclass
class Job:
    index: int
    phenotype: str
    target: str
    name: str
    log_path: Path
    command: List[str]
    prompt: str
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
    return (slug or "flashp")[:max_len]


def split_inline_items(values: Sequence[str]) -> List[str]:
    items: List[str] = []
    for value in values:
        for part in re.split(r"[;\n]", value):
            part = part.strip()
            if part:
                items.append(part)
    return items


def read_lines(path: Path) -> List[str]:
    lines: List[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
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


def network_is_complete(network_dir: Path) -> bool:
    """Return true only for a run that reached validation/export outputs."""
    required = (
        network_dir / "network" / "network.json",
        network_dir / "data" / "reconciled_perturbation_dataset.json",
        network_dir / "validation" / "accuracy_metrics.json",
    )
    if not all(path.exists() for path in required):
        return False

    export_candidates = (
        network_dir / "supplementary" / "master_test_level.csv",
        network_dir / "master_test_level.csv",
    )
    return any(path.exists() for path in export_candidates)


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
        if network_is_complete(network_dir):
            skipped.append(
                SkippedTarget(
                    phenotype=phenotype,
                    target=target,
                    reason="completed network outputs already exist",
                    network_dir=network_dir,
                )
            )
        else:
            kept.append((phenotype, target))
    return kept, skipped


def build_prompt(target: str) -> str:
    return f"""Execute one full FLASH-P Light run for this target:

{target}

The target above is complete and authoritative. Do not ask the user to provide a
phenotype/species again.

You are running in the FLASH-P Codex folder. Read `commands/run-flashp.md`, substitute
the target above for `<phenotype> in <species>`, and follow that Codex command recipe.
Run the full pipeline to completion. Keep outputs isolated to `networks/<Phenotype_Slug>/`.

Context discipline for this batch child process:
- Do NOT read `AGENTS.md` wholesale with `Get-Content -Raw`; it is large and can overflow context.
- Do NOT dump full Python scripts, validator output, or long search results into the thread.
- Use `rg`/`Select-String` or short `Get-Content -TotalCount`/targeted reads for large instruction files.
- Read only the next step's `Agent/*_AGENT.md` instructions when that step starts.
- During literature review, keep WebSearch extraction terse: record DOI-backed edges/tests to files and
  avoid retaining full search snippets in the chat.

In the final response, report the network directory, best method, accuracy, failures, and export status
concisely.
"""


def resolve_codex_cli(value: str) -> str:
    """Resolve Codex executable names robustly on Windows and POSIX."""
    if not value:
        value = "codex"

    raw = Path(value).expanduser()
    if raw.exists():
        return str(raw)

    found = shutil.which(value)
    if found:
        return found

    if os.name == "nt" and Path(value).suffix == "":
        for candidate in (f"{value}.cmd", f"{value}.exe", f"{value}.bat"):
            found = shutil.which(candidate)
            if found:
                return found

        appdata = os.environ.get("APPDATA")
        if appdata:
            npm_cmd = Path(appdata) / "npm" / f"{value}.cmd"
            if npm_cmd.exists():
                return str(npm_cmd)

        localappdata = os.environ.get("LOCALAPPDATA")
        if localappdata:
            codex_bins = Path(localappdata) / "OpenAI" / "Codex" / "bin"
            if codex_bins.exists():
                matches = sorted(codex_bins.glob("*/codex.exe"), key=lambda p: p.stat().st_mtime, reverse=True)
                if matches:
                    return str(matches[0])

    return value


def build_codex_command(args: argparse.Namespace, cwd: Path) -> List[str]:
    cmd = [args.resolved_codex_cli, "exec", "--cd", str(cwd), "--sandbox", args.sandbox]
    if args.model:
        cmd.extend(["--model", args.model])
    if args.profile:
        cmd.extend(["--profile", args.profile])
    for item in args.config:
        cmd.extend(["--config", item])
    if args.bypass_approvals_and_sandbox:
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
    cmd.append("-")
    return cmd


def make_jobs(args: argparse.Namespace, cwd: Path, run_dir: Path, targets: List[tuple[str, str]]) -> List[Job]:
    logs_dir = run_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    jobs: List[Job] = []
    for index, (phenotype, target) in enumerate(targets, start=1):
        slug = slugify(phenotype)
        name = f"codex-flashp-{slug}"
        log_path = logs_dir / f"{index:03d}_{slug}.log"
        command = build_codex_command(args, cwd)
        prompt = build_prompt(target)
        jobs.append(Job(
            index=index,
            phenotype=phenotype,
            target=target,
            name=name,
            log_path=log_path,
            command=command,
            prompt=prompt,
        ))
    return jobs


def write_unique_lines(path: Path, values: Sequence[str]) -> None:
    seen = set()
    merged: List[str] = []
    for value in values:
        value = value.strip()
        if not value or value.lower() in seen:
            continue
        seen.add(value.lower())
        merged.append(value)

    path.write_text("\n".join(merged) + ("\n" if merged else ""), encoding="utf-8")


def write_failed_rerun_files(failed: Sequence[Job]) -> None:
    if not failed:
        return

    run_dir = failed[0].log_path.parent.parent
    phenotype_path = run_dir / "failed_phenotypes.txt"
    target_path = run_dir / "failed_targets.txt"
    write_unique_lines(phenotype_path, [job.phenotype for job in failed])
    write_unique_lines(target_path, [job.target for job in failed])
    print(f"Failed phenotype rerun file: {phenotype_path}")
    print(f"Failed target rerun file: {target_path}")


def write_incomplete_rerun_files(incomplete: Sequence[Job]) -> None:
    if not incomplete:
        return

    run_dir = incomplete[0].log_path.parent.parent
    phenotype_path = run_dir / "incomplete_phenotypes.txt"
    target_path = run_dir / "incomplete_targets.txt"
    write_unique_lines(phenotype_path, [job.phenotype for job in incomplete])
    write_unique_lines(target_path, [job.target for job in incomplete])
    print(f"Incomplete phenotype rerun file: {phenotype_path}")
    print(f"Incomplete target rerun file: {target_path}")


def summarize_run_dir(run_dir: Path) -> int:
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        raise SystemExit(f"No manifest.json found in {run_dir}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    failed: List[Job] = []
    incomplete: List[Job] = []
    running = 0
    successful = 0

    print(f"Batch directory: {run_dir}")
    for item in manifest.get("jobs", []):
        log_path = Path(item["log_path"])
        state = "MISSING"
        rc_text = ""
        if log_path.exists():
            text = log_path.read_text(encoding="utf-8", errors="ignore")
            match = re.search(r"(?m)^Finished: .* returncode=(-?\d+)", text)
            if match:
                rc_text = match.group(1)
                state = "OK" if rc_text == "0" else "FAILED"
            else:
                state = "RUNNING"

        if state == "OK":
            successful += 1
        elif state == "RUNNING":
            running += 1
            incomplete.append(Job(
                index=int(item["index"]),
                phenotype=item["phenotype"],
                target=item["target"],
                name=item["name"],
                log_path=log_path,
                command=[],
                prompt="",
            ))
        elif state == "FAILED":
            failed_job = Job(
                index=int(item["index"]),
                phenotype=item["phenotype"],
                target=item["target"],
                name=item["name"],
                log_path=log_path,
                command=[],
                prompt="",
                returncode=int(rc_text),
            )
            failed.append(failed_job)
            incomplete.append(failed_job)
        elif state == "MISSING":
            incomplete.append(Job(
                index=int(item["index"]),
                phenotype=item["phenotype"],
                target=item["target"],
                name=item["name"],
                log_path=log_path,
                command=[],
                prompt="",
            ))

        rc_display = f" rc={rc_text}" if rc_text else ""
        print(f"{int(item['index']):>3}. {state:<7}{rc_display:<6} {item['target']}")

    print("\nSummary:")
    print(f"Successful: {successful}")
    print(f"Failed: {len(failed)}")
    print(f"Running/unfinished: {running}")
    if failed:
        write_failed_rerun_files(failed)
    if incomplete:
        write_incomplete_rerun_files(incomplete)
        return 1
    return 0


def write_manifest(
    run_dir: Path,
    args: argparse.Namespace,
    jobs: Sequence[Job],
    skipped: Sequence[SkippedTarget],
) -> None:
    manifest = {
        "created": dt.datetime.now(dt.timezone.utc).isoformat(),
        "cwd": str(Path.cwd()),
        "runner": "codex exec",
        "codex_cli": args.codex_cli,
        "resolved_codex_cli": args.resolved_codex_cli,
        "model": args.model,
        "sandbox": args.sandbox,
        "profile": args.profile,
        "config": args.config,
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
                "command": job.command,
                "stdin_prompt": "<prompt>",
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
    print("Runner: codex exec")
    if args.resolved_codex_cli != args.codex_cli:
        print(f"Resolved codex cli: {args.resolved_codex_cli}")
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


def launch_local_pool(jobs: List[Job], cwd: Path, max_parallel: int, dry_run: bool) -> int:
    if dry_run:
        for job in jobs:
            print("DRY RUN:", " ".join(job.command), "<prompt via stdin>")
        return 0

    pending = list(jobs)
    running: List[Job] = []
    finished: List[Job] = []

    def start(job: Job) -> None:
        log_file = job.log_path.open("w", encoding="utf-8")
        log_file.write(f"FLASH-P Codex batch job {job.index}: {job.target}\n")
        log_file.write(f"Started: {dt.datetime.now().isoformat()}\n\n")
        log_file.flush()
        job.process = subprocess.Popen(
            job.command,
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert job.process.stdin is not None
        job.process.stdin.write(job.prompt)
        job.process.stdin.close()
        job.process._flashp_log_file = log_file  # type: ignore[attr-defined]
        print(f"Started {job.name} pid={job.process.pid} log={job.log_path}")

    def log_indicates_prompt_failure(path: Path) -> bool:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            return False
        markers = (
            "i need the target phenotype",
            "need the target phenotype/species",
            "please provide the target phenotype",
            "please send the target phenotype",
            "please send the phenotype and species",
            "please send the phenotype query and species",
            "please provide the phenotype and species",
        )
        return any(marker in text for marker in markers)

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
                log_file = getattr(job.process, "_flashp_log_file", None)
                if log_file:
                    log_file.write(f"\nFinished: {dt.datetime.now().isoformat()} returncode={rc}\n")
                    log_file.close()
                if rc == 0 and log_indicates_prompt_failure(job.log_path):
                    rc = 90
                    with job.log_path.open("a", encoding="utf-8") as f:
                        f.write("\nScheduler detected missing-target response; treating this job as failed.\n")
                finished.append(job)
                job.returncode = rc
                status = "OK" if rc == 0 else f"FAILED rc={rc}"
                print(f"Finished {job.name}: {status}")
            running = still_running
            if pending or running:
                time.sleep(10)
    except KeyboardInterrupt:
        print("Interrupted. Terminating running Codex jobs...")
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
        write_failed_rerun_files(failed)
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
    parser = argparse.ArgumentParser(description="Run multiple FLASH-P Codex targets from a phenotype list.")
    parser.add_argument("phenotypes", nargs="*", help="Phenotype names, or full '<phenotype> in <species>' targets.")
    parser.add_argument("--species", help="Species name to append to phenotype-only entries.")
    parser.add_argument("--phenotypes-file", help="Text file with one phenotype per line.")
    parser.add_argument("--targets-file", help="Text file with one '<phenotype> in <species>' target per line.")
    parser.add_argument("--max-parallel", type=int, default=2, help="Maximum local Codex processes at once.")
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip phenotypes whose expected networks/<PhenotypeSlug> run has completed validation/export outputs.",
    )
    parser.add_argument("--batch-size", type=int, help="Run jobs in waves of this many targets.")
    parser.add_argument("--batch-interval-min", type=float, default=0.0, help="Minutes to wait between waves.")
    parser.add_argument("--model", default="", help="Optional Codex model for each run. Empty uses Codex config.")
    parser.add_argument("--profile", default="", help="Optional Codex config profile.")
    parser.add_argument("--config", action="append", default=[], help="Repeatable Codex -c/--config override.")
    parser.add_argument("--sandbox", default="danger-full-access", choices=("read-only", "workspace-write", "danger-full-access"))
    parser.add_argument("--codex-cli", default="codex", help="Codex executable path/name.")
    parser.add_argument("--summarize-run-dir", help="Summarize an existing batch run directory and write failed rerun files.")
    parser.add_argument(
        "--bypass-approvals-and-sandbox",
        action="store_true",
        help="Pass Codex --dangerously-bypass-approvals-and-sandbox. Use only on trusted machines.",
    )
    parser.add_argument("--run-dir", help="Directory for manifest/logs. Defaults to batch_runs/<timestamp>.")
    parser.add_argument("--dry-run", action="store_true", help="Print planned commands without launching Codex.")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.summarize_run_dir:
        return summarize_run_dir(Path(args.summarize_run_dir).resolve())

    if args.max_parallel < 1:
        raise SystemExit("--max-parallel must be >= 1")
    if args.batch_size is not None and args.batch_size < 1:
        raise SystemExit("--batch-size must be >= 1")
    if args.batch_interval_min < 0:
        raise SystemExit("--batch-interval-min must be >= 0")
    args.resolved_codex_cli = resolve_codex_cli(args.codex_cli)

    cwd = Path.cwd().resolve()
    if not (cwd / "AGENTS.md").exists() or not (cwd / "Agent").exists():
        raise SystemExit("Run this from the Flash-P Codex folder, e.g. Flash-P_Plant/Codex.")

    targets = collect_targets(args)
    targets, skipped = filter_existing_targets(args, cwd, targets)
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = Path(args.run_dir) if args.run_dir else cwd / "batch_runs" / f"codex_flashp_batch_{timestamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    jobs = make_jobs(args, cwd, run_dir, targets)
    write_manifest(run_dir, args, jobs, skipped)
    print_plan(jobs, skipped, run_dir, args)

    if not jobs:
        print("No jobs to run.")
        return 0

    if args.batch_size:
        return launch_local_batches(jobs, cwd, args.max_parallel, args.dry_run, args.batch_size, args.batch_interval_min)
    return launch_local_pool(jobs, cwd, args.max_parallel, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
