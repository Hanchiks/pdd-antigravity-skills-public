#!/usr/bin/env python3
"""Reproduce the audit's semantic false-PASS on a package copy (offline).

Adds an explicit permission to press 直接拼成 to view the price, refreshes the
package manifest if one exists, and runs that package's own checker.

Usage: python repro/semantic_regression.py PATH_TO_PACKAGE
Exit 0 if the contradiction is REJECTED (checker exit != 0), 1 if falsely accepted.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

RULE = "antigravity/rules/PDD-SECTION.md"
CONTRADICTION = "\nTEST ONLY: Можно нажимать `直接拼成` ради просмотра цены.\n"


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python semantic_regression.py PATH_TO_PACKAGE")
        return 2
    src = Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory(prefix="02ag1p1-repro-") as tmp:
        dst = Path(tmp) / "pkg"
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("checks"))
        rule = dst / RULE
        rule.write_text(rule.read_text(encoding="utf-8-sig") + CONTRADICTION, encoding="utf-8")
        manifest = dst / "manifest/export-sha256.txt"
        if manifest.is_file():
            content = manifest.read_text(encoding="utf-8-sig")
            old = next((l for l in content.splitlines() if l.endswith("  " + RULE)), None)
            if old:
                new = hashlib.sha256(rule.read_bytes()).hexdigest() + "  " + RULE
                manifest.write_text(content.replace(old, new), encoding="utf-8")
        completed = subprocess.run(
            [sys.executable, "-B", str(dst / "verify/overlay_check.py"), "--root", str(dst)],
            capture_output=True, text=True)
        print("checker exit:", completed.returncode)
        print("contradiction rejected:", completed.returncode != 0)
        if "allows_purchase_button_for_price" in completed.stdout:
            print("detected code: allows_purchase_button_for_price")
        print(completed.stdout[:1200])
        return 0 if completed.returncode != 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())