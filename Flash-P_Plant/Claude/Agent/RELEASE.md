# Cutting a Flash-P release

Flash-P's version is git-tag-derived — see `Agent/shared/flashp_version.py`. There is nothing to
hand-edit day to day; this doc is only for the moment you actually want to mark a release.

## Preconditions

1. You're on `main`.
2. `git status --porcelain` is empty (clean working tree).
3. `git fetch && git log HEAD..origin/main --oneline` is empty (you're in sync with the remote).

## Steps

1. **Add a CHANGELOG entry.** In `Agent/CHANGELOG.md`, add a new section at the top:
   ```markdown
   ## [vX.Y.Z] - YYYY-MM-DD

   ### Added / Changed / Fixed
   - ...
   ```
   Commit it: `git commit -m "Prepare vX.Y.Z release notes"`.

2. **Dry-run the release script** to see exactly what it would do:
   ```bash
   python Agent/shared/cut_release.py X.Y.Z
   ```
3. **Execute it** once the dry-run output looks right:
   ```bash
   python Agent/shared/cut_release.py X.Y.Z --execute
   ```
   This tags `HEAD` as `vX.Y.Z`, pushes the tag to `origin`, and creates a GitHub Release
   (via `gh release create`) using the matching `## [vX.Y.Z]` section of `CHANGELOG.md` as the
   release notes.

4. **Verify:**
   ```bash
   git fetch --tags && git describe --tags   # should print vX.Y.Z
   gh release view vX.Y.Z                    # should show it live on GitHub
   ```
   Optionally rebuild a Studio locally (`python Agent/shared/network_to_studio.py networks`) and
   confirm the browse-page subtitle shows the new version.

## What picks a version number

Flash-P follows plain semver (`vMAJOR.MINOR.PATCH`, no build-variant suffix — `build_variant` is
a separate metadata field, e.g. `"debiasing"`, orthogonal to the version number):
- **PATCH** — bug fixes, no behavior/output-shape change.
- **MINOR** — new, backward-compatible capability (new pipeline step, new output field, new agent).
- **MAJOR** — breaking change (output schema change that isn't backward-compatible, removed step).

## Notes

- `cut_release.py` refuses to move an existing tag and defaults to `--dry-run` — nothing pushes
  or creates a public release unless you pass `--execute` explicitly.
- The static `Agent/shared/VERSION` fallback file (used only when `.git` isn't available, e.g. a
  zipped copy shipped without history) is refreshed automatically by `cut_release.py --execute`.
  Never hand-edit it.
- The `v1.0.0` tag was cut as a one-off baseline directly with `git tag`/`git push`/`gh release
  create` (documented in `Agent/CHANGELOG.md`), before `cut_release.py` existed. Every release
  after it should go through this script.
