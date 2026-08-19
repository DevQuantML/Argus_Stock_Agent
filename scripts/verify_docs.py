#!/usr/bin/env python
"""
verify_docs.py — CLAUDE.md architecture table stays in sync with the tree.

The architecture block in CLAUDE.md is the first thing a contributor (human or
AI) reads. When it drifts, modules get listed as absent and deleted as "dead
code", or real files go undocumented and invisible. This script makes the drift
visible before it causes damage.

Guarded by two directions:

  Forward (doc → disk): every *.py file named in the architecture block must
  exist in the tree. A typo or a renamed module fails here.

  Reverse (disk → doc): every *.py file under tools/ and every
  scripts/verify_*.py must appear somewhere in the architecture block. A new
  module added without a doc entry fails here.

Why this harness exists
-----------------------
tools/fundamentals.py, fx.py, xirr.py and store.py were all absent from the
architecture table across multiple sessions despite being live, imported
dependencies. An AI assistant reading CLAUDE.md would have thought them dead
code and deleted them. The drift was caught by a manual post-publish audit
rather than an automated check.

Cost
----
Free. No network, no API calls, no yfinance. Reads two local files only.

Usage
-----
    python scripts/verify_docs.py

Exit codes
----------
    0   every check passed
    1   a check failed
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    status = "ok" if ok else "FAIL"
    print(f"  [{status}] {name:<48} {detail}".rstrip())


def main() -> None:
    claude_md = ROOT / "CLAUDE.md"
    if not claude_md.exists():
        print("FATAL: CLAUDE.md not found")
        sys.exit(1)

    text = claude_md.read_text(encoding="utf-8")

    # Extract the fenced code block immediately following "## Architecture".
    # The regex is non-greedy so it stops at the first closing fence.
    m = re.search(r"## Architecture\s*\n+```(.*?)```", text, re.DOTALL)
    if not m:
        print("FATAL: no fenced architecture block found under '## Architecture' in CLAUDE.md")
        sys.exit(1)

    block = m.group(1)

    print("\nforward check — every *.py named in the architecture block must exist")
    # Each non-empty line in the block has the form:
    #   filename.py        description text that may itself name other .py files
    # Only the first whitespace-delimited token on the line is the declared path;
    # anything after the first gap is description prose. Scanning the full line
    # with a regex would pick up "quant.py" from "(imported by quant.py)" in the
    # fundamentals.py description, which is not a path declaration.
    seen: set[str] = set()
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        first = stripped.split()[0]
        if first.endswith(".py") and first not in seen and "*" not in first:
            seen.add(first)
            path = ROOT / first
            check(f"exists: {first}", path.exists())

    print("\nreverse check — every tools/*.py must appear in the block")
    tools_dir = ROOT / "tools"
    for f in sorted(tools_dir.glob("*.py")):
        if f.name == "__init__.py":
            # Not a module consumers import directly; not listed in the table.
            continue
        check(f"documented: tools/{f.name}", f.name in block)

    print("\nreverse check — every scripts/verify_*.py must be covered")
    # The architecture block uses the wildcard form "scripts/verify_*.py" to
    # cover them collectively. If that wildcard is gone someone replaced it with
    # a per-script list; the individual names are then the right thing to check.
    wildcard_covered = "verify_*.py" in block
    for f in sorted(ROOT.glob("scripts/verify_*.py")):
        individual_covered = f.name in block
        check(
            f"documented: scripts/{f.name}",
            wildcard_covered or individual_covered,
            "(covered by wildcard)" if wildcard_covered and not individual_covered else "",
        )

    print("\nspot checks — key entries that have drifted before")
    for entry in ["store.py", "docs/"]:
        check(f"documented: {entry}", entry in block)


main()

for n in PASS:
    pass  # already printed inline
for n in FAIL:
    pass  # already printed inline
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
