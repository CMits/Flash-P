#!/usr/bin/env python3
"""
Cut a Flash-P release: tag HEAD, push the tag, create a GitHub Release.

Defaults to --dry-run (prints exactly what it would run). Nothing pushes or
creates a public release unless --execute is passed explicitly. See
Agent/RELEASE.md for the full procedure.

Usage:
    python Agent/shared/cut_release.py 1.1.0 [--execute] [--allow-dirty] [--remote origin]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CHANGELOG = Path(__file__).resolve().parents[1] / "CHANGELOG.md"
VERSION_FILE = Path(__file__).resolve().parent / "VERSION"

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def run(args: list, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=REPO_ROOT, capture_output=True, text=True, check=check)


def git(*args: str) -> str:
    return run(["git", *args]).stdout.strip()


def tag_exists_locally(tag: str) -> bool:
    r = run(["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}"], check=False)
    return r.returncode == 0


def tag_exists_on_remote(tag: str, remote: str) -> bool:
    r = run(["git", "ls-remote", "--tags", remote, tag], check=False)
    return bool(r.stdout.strip())


def extract_changelog_section(version: str) -> str:
    """Pull the '## [vX.Y.Z] - ...' section out of CHANGELOG.md, if present."""
    if not CHANGELOG.exists():
        return ""
    text = CHANGELOG.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"^## \[v{re.escape(version)}\].*?$(.*?)(?=^## \[|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(text)
    return m.group(0).strip() if m else ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="Bare semver, e.g. 1.1.0 (no leading 'v')")
    parser.add_argument("--execute", action="store_true",
                         help="Actually run the commands (default: dry-run, print only)")
    parser.add_argument("--allow-dirty", action="store_true",
                         help="Skip the clean-working-tree check")
    parser.add_argument("--remote", default="origin", help="Git remote to push to (default: origin)")
    args = parser.parse_args()

    version = args.version
    if not SEMVER_RE.match(version):
        print(f"Error: {version!r} is not plain semver (expected X.Y.Z, no 'v' prefix)",
              file=sys.stderr)
        return 2
    tag = f"v{version}"

    if not args.allow_dirty:
        status = git("status", "--porcelain")
        if status:
            print("Error: working tree is not clean. Commit/stash first, or pass --allow-dirty.",
                  file=sys.stderr)
            print(status, file=sys.stderr)
            return 2

    if tag_exists_locally(tag):
        print(f"Error: tag {tag} already exists locally. Refusing to move an existing tag.",
              file=sys.stderr)
        return 2
    if tag_exists_on_remote(tag, args.remote):
        print(f"Error: tag {tag} already exists on remote {args.remote!r}. "
              f"Refusing to move an existing tag.", file=sys.stderr)
        return 2

    head = git("rev-parse", "HEAD")
    notes = extract_changelog_section(version)

    print(f"Would cut release {tag} at {head[:12]}")
    print(f"  1. git tag -a {tag} -m \"{tag}\"")
    print(f"  2. git push {args.remote} {tag}")
    if notes:
        print(f"  3. gh release create {tag} --title {tag} --notes-file <tmp file with "
              f"the '## [{tag}]' CHANGELOG section, {len(notes)} chars>")
    else:
        print(f"  3. gh release create {tag} --title {tag} --generate-notes  "
              f"(no '## [{tag}]' section found in {CHANGELOG.name} — using GitHub's auto-notes)")
    print(f"  4. Refresh {VERSION_FILE.relative_to(REPO_ROOT)} to contain: {version}")

    if not args.execute:
        print("\nDry-run only — pass --execute to actually run these steps.")
        return 0

    print("\nExecuting...")
    run(["git", "tag", "-a", tag, "-m", tag])
    print(f"  tagged {tag}")
    run(["git", "push", args.remote, tag])
    print(f"  pushed {tag} to {args.remote}")

    if notes:
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(notes)
            notes_path = f.name
        run(["gh", "release", "create", tag, "--title", tag, "--notes-file", notes_path])
    else:
        run(["gh", "release", "create", tag, "--title", tag, "--generate-notes"])
    print(f"  created GitHub Release {tag}")

    VERSION_FILE.write_text(version + "\n", encoding="utf-8")
    print(f"  refreshed {VERSION_FILE.relative_to(REPO_ROOT)} -> {version}")

    print(f"\nDone. https://github.com/CMits/Flash-P/releases/tag/{tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
