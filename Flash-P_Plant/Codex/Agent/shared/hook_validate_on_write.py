#!/usr/bin/env python3
"""
FLASH-P PostToolUse hook: schema-validate a pipeline JSON file right after it is
written/edited, so a schema violation is caught immediately instead of surfacing
turns later in a validator crash.

Design goals (safety first — there is no git here to revert a bad write):
  * Acts ONLY on FLASH-P pipeline JSON files (under data/ network/ validation/
    refinement/). Everything else -> exit 0, do nothing.
  * NEVER crashes a write: any unexpected error -> exit 0 (silent pass-through).
  * Silent on success (no output, no added context tokens).
  * On a genuine schema FAILURE -> print the error to stderr and exit 2, which
    tells Claude Code to surface it so the model fixes the file immediately.

Wire-up (in .claude/settings.json):
  "hooks": { "PostToolUse": [ { "matcher": "Write|Edit",
    "hooks": [ { "type": "command",
      "command": "python \"Agent/shared/hook_validate_on_write.py\"" } ] } ] }

Reads the PostToolUse event JSON on stdin; uses tool_input.file_path.
"""
import json
import os
import subprocess
import sys

PIPELINE_SEGMENTS = ("/data/", "/network/", "/validation/", "/refinement/")


def main() -> int:
    # 1. Parse the hook payload. Any problem -> pass through silently.
    try:
        event = json.load(sys.stdin)
    except Exception:
        return 0

    tool_input = event.get("tool_input") or {}
    fp = tool_input.get("file_path") or tool_input.get("path")
    if not fp or not str(fp).endswith(".json"):
        return 0

    # 2. Only validate files that live in a FLASH-P pipeline directory.
    norm = str(fp).replace("\\", "/")
    if not any(seg in norm for seg in PIPELINE_SEGMENTS):
        return 0

    # 3. Run the schema validator on that one file.
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "validate_schema.py")
    if not os.path.exists(script) or not os.path.exists(fp):
        return 0
    try:
        r = subprocess.run(
            [sys.executable, script, fp],
            capture_output=True, text=True, timeout=60,
        )
    except Exception:
        return 0  # validator unavailable / timed out -> never block the write

    # 4. exit code 0 = PASS (or unrecognized/skip) -> stay silent.
    if r.returncode == 0:
        return 0

    # 5. Real validation failure -> surface to Claude and block so it gets fixed.
    msg = (r.stdout + "\n" + r.stderr).strip()
    print(f"[FLASH-P schema check] {os.path.basename(fp)} FAILED validation:\n{msg}",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
