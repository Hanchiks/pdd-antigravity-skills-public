#!/usr/bin/env python3
"""Run the working and experimental operator test suites separately.

Offline only: each suite uses the synthetic environment prepared by
``runtime/test_env.py``. No ADB, browser, network or real account is touched.

The two versions are never merged:

* ``runtime/pdd.py`` -- working version;
* ``runtime/experimental/pdd-02d1f/pdd.py`` -- candidate that is *not*
  accepted. There is no documented justification for promoting it into the
  working version, so this script only verifies it in its own directory.

The script prints the file and working directory for every version, runs each
suite in a separate subprocess, keeps going if one fails, and exits non-zero
if any suite fails.
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


@dataclass(frozen=True)
class VersionCheck:
    label: str
    file: Path
    workdir: Path
    command: tuple[str, ...]
    env: dict[str, str] | None = None


def _experimental_env() -> dict[str, str]:
    env = dict(os.environ)
    parts = [".", os.path.join("..", "..")]
    existing = env.get("PYTHONPATH")
    if existing:
        parts.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


CHECKS = (
    VersionCheck(
        label="working version (accepted)",
        file=REPO_ROOT / "runtime" / "pdd.py",
        workdir=REPO_ROOT,
        command=(PYTHON, "-m", "unittest", "discover", "-s", "runtime/tests", "-t", "runtime"),
    ),
    VersionCheck(
        label="experimental candidate (NOT accepted, no promotion basis)",
        file=REPO_ROOT / "runtime" / "experimental" / "pdd-02d1f" / "pdd.py",
        workdir=REPO_ROOT / "runtime" / "experimental" / "pdd-02d1f",
        command=(PYTHON, "-m", "unittest", "discover", "-s", "tests", "-t", "."),
        env=_experimental_env(),
    ),
)


def run_check(check: VersionCheck) -> bool:
    print("=" * 78)
    print(f"[version] {check.label}")
    print(f"[file]    {check.file}")
    print(f"[cwd]     {check.workdir}")
    print(f"[run]     {' '.join(check.command)}")
    if not check.file.is_file():
        print(f"[FAIL]    file not found: {check.file}")
        return False
    try:
        completed = subprocess.run(
            list(check.command),
            cwd=str(check.workdir),
            env=check.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except OSError as exc:
        print(f"[FAIL]    could not start interpreter: {exc}")
        return False
    if completed.stdout:
        print(completed.stdout.rstrip())
    ok = completed.returncode == 0
    print(f"[result]  {'OK' if ok else 'FAILED'} (exit {completed.returncode})")
    return ok


def main() -> int:
    print("Independent offline verification of operator versions.")
    print("The experimental candidate is not promoted by this script.")
    results = [(check, run_check(check)) for check in CHECKS]
    print("=" * 78)
    for check, ok in results:
        status = "OK" if ok else "FAILED"
        print(f"{status:6} {check.label}: {check.file}")
    failed = [check for check, ok in results if not ok]
    if failed:
        print(f"\nFAILED: {len(failed)} of {len(results)} version suites.")
        return 1
    print(f"\nOK: {len(results)} of {len(results)} version suites passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())