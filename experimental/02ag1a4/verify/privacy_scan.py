#!/usr/bin/env python3
"""Privacy overlap scan (counts only).

Reads a private local reference (default: the live Antigravity PDD rule) and
counts how many of its non-trivial lines appear in every staged file and in
every patch hunk (including removed lines). It never prints or writes the
reference lines themselves; only aggregate counts are reported.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

MIN_LEN = 12


def reference_lines(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    text = path.read_text(encoding="utf-8-sig", errors="ignore")
    return {line.strip() for line in text.splitlines() if len(line.strip()) >= MIN_LEN}


def patch_recovered_lines(patch_text: str) -> set[str]:
    """Lines appearing in a hunk on the ' ' or '-' side (|line| >= MIN_LEN)."""
    out = set()
    for raw in patch_text.splitlines():
        if raw.startswith("--- ") or raw.startswith("+++ ") or raw.startswith("@@"):
            continue
        if raw[:1] in (" ", "-"):
            candidate = raw[1:].strip()
            if len(candidate) >= MIN_LEN:
                out.add(candidate)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    ap.add_argument("--reference", type=Path,
                    default=Path.home() / ".gemini" / "config" / "rules" / "AGENTS.md")
    args = ap.parse_args(argv)
    root = args.root.resolve()
    ref = reference_lines(args.reference)
    if not ref:
        print(f"reference unavailable or empty: {args.reference} (counts skipped)")
        return 2

    total_hits = 0
    files_with_hits = 0
    details = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(root).as_posix()
        if rel.startswith("checks/"):
            continue
        try:
            text = path.read_text(encoding="utf-8-sig", errors="ignore")
        except OSError:
            continue
        hits = len(ref & {line.strip() for line in text.splitlines()})
        if path.suffix == ".patch":
            hits = max(hits, len(ref & patch_recovered_lines(text)))
        if hits:
            files_with_hits += 1
            total_hits += hits
            details.append((rel, hits))
    print(f"reference_lines={len(ref)} files_with_overlap={files_with_hits} total_overlap={total_hits}")
    for rel, hits in details:
        print(f"  {rel}: {hits} overlapping lines (content not shown)")
    return 0 if total_hits == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())