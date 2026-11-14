#!/usr/bin/env python3
"""Read-only pre-state inventory for the approved Antigravity PDD targets.

02AG1A2: the root must exist and be a directory; every ancestor from the
filesystem anchor down to the root is inspected for symlink/junction/reparse
before any resolution; results carry a canonical root identity and a
timezone-aware timestamp so the planner can attest provenance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from transaction_plan import CANONICAL_ROOT, allowlist  # noqa: E402


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def is_reparse(path: Path) -> bool:
    try:
        st = path.lstat()
    except OSError:
        return False
    return bool(stat.S_ISLNK(st.st_mode)) or bool(
        getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def reparse_in_chain(root: Path) -> bool:
    """Check every component from the filesystem anchor down to root, inclusive."""
    anchor = Path(root.anchor) if root.anchor else Path(root.parts[0])
    current = anchor
    for part in root.parts[1:]:
        current = current / part
        if is_reparse(current):
            return True
    return is_reparse(root)


def root_identity(root: Path) -> str:
    """Physical absolute root identity; a name ending in .gemini/config proves nothing."""
    try:
        return str(root.resolve())
    except OSError:
        return str(root)


def relative_to_root(target: str) -> str | None:
    prefix = "~/.gemini/config/"
    if not target.startswith(prefix):
        return None
    return target[len(prefix):]


def inventory(root: Path, targets: list[str], *, now: datetime | None = None) -> dict:
    allowed = allowlist()
    generated = now or datetime.now(timezone.utc)
    identity = root_identity(root)

    def failed(code: str) -> dict:
        blocked = {t: {"exists": None, "sha256": None, "error": code} for t in targets}
        return {"ok": False, "errors": {t: code for t in targets}, "root": identity,
                "generated_at": generated.isoformat(), "targets": blocked}

    if not root.exists() or not root.is_dir():
        return failed("root_unavailable")
    if reparse_in_chain(root):
        return failed("reparse_point")

    resolved = root.resolve()
    result = {}
    for target in targets:
        if target not in allowed:
            result[target] = {"exists": None, "sha256": None, "error": "target_out_of_scope"}
            continue
        rel = relative_to_root(target)
        if rel is None:
            result[target] = {"exists": None, "sha256": None, "error": "target_out_of_scope"}
            continue
        path = resolved / rel
        if reparse_in_chain(path):
            result[target] = {"exists": True, "sha256": None, "error": "reparse_point"}
            continue
        try:
            path.resolve().relative_to(resolved)
        except (ValueError, OSError):
            result[target] = {"exists": None, "sha256": None, "error": "outside_root"}
            continue
        if not path.exists():
            result[target] = {"exists": False, "sha256": None}
        elif not path.is_file():
            result[target] = {"exists": True, "sha256": None, "error": "not_a_file"}
        else:
            try:
                result[target] = {"exists": True, "sha256": sha256(path)}
            except OSError as exc:
                result[target] = {"exists": True, "sha256": None, "error": f"unreadable: {exc}"}
    errors = {t: e["error"] for t, e in result.items() if e.get("error")}
    return {"ok": not errors, "errors": errors, "root": identity, "generated_at": generated.isoformat(),
            "targets": result}


def _parse_now(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--now", help="ISO timestamp for deterministic tests; never a wall-clock substitute")
    args = ap.parse_args(argv)
    man = json.loads(args.manifest.read_text(encoding="utf-8-sig"))
    targets = [a["target"] for a in man.get("actions", [])]
    data = inventory(args.root, targets, now=_parse_now(args.now))
    errors = {t: e["error"] for t, e in data["targets"].items() if e.get("error")}
    ok = data["ok"] and not errors
    data["ok"] = ok
    data["errors"] = errors
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": ok, "targets": len(data["targets"]), "errors": errors,
                      "root": data["root"], "generated_at": data["generated_at"],
                      "output": str(args.output)}, ensure_ascii=False))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())


