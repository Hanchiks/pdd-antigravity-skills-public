#!/usr/bin/env python3
"""Transactional, read-only deployment planner (never writes to live config).

02AG1A2 release gate:
- the plan must cover exactly the 26 allowlisted targets (25 skills + one rule);
- the inventory must be positively attested: ok=true, errors={}, canonical root
  identity, and a timezone-aware timestamp within the freshness window;
- a replacement needs a real backup file under an independent backup root whose
  SHA-256 matches the current target hash; a claimed `verified` flag is not proof.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import posixpath
import sys
from datetime import datetime, timezone
from pathlib import Path

RULE_TARGET = "~/.gemini/config/rules/AGENTS.md"
SKILLS_PREFIX = "~/.gemini/config/skills/"
CANONICAL_ROOT = "~/.gemini/config"
DEFAULT_MAX_AGE_SECONDS = 900
CLOCK_SKEW_SECONDS = 300

ALLOWED_SKILL_TARGETS = frozenset({
    "~/.gemini/config/skills/adb-wifi-qr/SKILL.md",
    "~/.gemini/config/skills/adb-wifi-qr/agents/openai.yaml",
    "~/.gemini/config/skills/adb-wifi-qr/scripts/adb_wifi_qr.py",
    "~/.gemini/config/skills/adb-wifi-qr/scripts/test_adb_wifi_qr.py",
    "~/.gemini/config/skills/pdd-operator/SKILL.md",
    "~/.gemini/config/skills/pdd-operator/agents/openai.yaml",
    "~/.gemini/config/skills/pinduoduo-buyer/SKILL.md",
    "~/.gemini/config/skills/pinduoduo-buyer/README_RU.md",
    "~/.gemini/config/skills/pinduoduo-buyer/assets/PORTABLE_PROMPT_RU.md",
    "~/.gemini/config/skills/pinduoduo-buyer/assets/search-session.template.json",
    "~/.gemini/config/skills/pinduoduo-buyer/assets/user-profile.example.json",
    "~/.gemini/config/skills/pinduoduo-buyer/references/acceptance-tests.md",
    "~/.gemini/config/skills/pinduoduo-buyer/references/category-profiles.md",
    "~/.gemini/config/skills/pinduoduo-buyer/references/chinese-glossary.md",
    "~/.gemini/config/skills/pinduoduo-buyer/references/decision-policy.md",
    "~/.gemini/config/skills/pinduoduo-buyer/references/device-and-permissions.md",
    "~/.gemini/config/skills/pinduoduo-buyer/references/evidence-audit.md",
    "~/.gemini/config/skills/pinduoduo-buyer/references/intake-and-categories.md",
    "~/.gemini/config/skills/pinduoduo-buyer/references/marketplace-playbook.md",
    "~/.gemini/config/skills/pinduoduo-buyer/references/output-contract.md",
    "~/.gemini/config/skills/pinduoduo-buyer/references/shipping-profile.md",
    "~/.gemini/config/skills/pinduoduo-buyer/scripts/compare_prices.py",
    "~/.gemini/config/skills/pinduoduo-buyer/scripts/test_compare_prices.py",
    "~/.gemini/config/skills/pinduoduo-buyer/scripts/test_validate_offer_evidence.py",
    "~/.gemini/config/skills/pinduoduo-buyer/scripts/validate_offer_evidence.py",
})

ALLOWED_TARGETS = ALLOWED_SKILL_TARGETS | {RULE_TARGET}
ALLOWED_ACTIONS = {"create", "replace", "rule_replace"}
BLOCKED_HASHES = {
    "0666ee5ce9227fe3ce2c45877d757df562dcee5b6b329323d7f9d4db99ec2792",
    "ceb8872b518e85fee987218eff62002926d4d9dc19995be08b4a90f9b05a7ba6",
}


def allowlist() -> frozenset[str]:
    return ALLOWED_TARGETS


def is_valid_sha(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdefABCDEF" for c in value)


def validate_target(target: str) -> str | None:
    if not isinstance(target, str) or not target:
        return "malformed_target"
    if "\\" in target:
        return "alternate_separator"
    if target.startswith(("/", "//")) or target.upper().startswith("\\\\"):
        return "absolute_or_unc"
    if ".." in target.split("/"):
        return "path_traversal"
    if posixpath.normpath(target) != target:
        return "non_normalized_path"
    if target not in ALLOWED_TARGETS:
        return "target_out_of_scope"
    return None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_reparse(path: Path) -> bool:
    try:
        st = path.lstat()
    except OSError:
        return False
    import stat as _stat
    return bool(_stat.S_ISLNK(st.st_mode)) or bool(
        getattr(st, "st_file_attributes", 0) & getattr(_stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _chain_reparse(path: Path) -> bool:
    anchor = Path(path.anchor) if path.anchor else Path(path.parts[0])
    current = anchor
    for part in path.parts[1:]:
        current = current / part
        if _is_reparse(current):
            return True
    return _is_reparse(path)


def validate_backup_relative_path(rel) -> str | None:
    """Strict lexical validation of a backup-relative path (no filesystem access)."""
    if not isinstance(rel, str) or not rel:
        return "backup_out_of_scope"
    if "\\" in rel:
        return "backup_out_of_scope"
    if rel.startswith("/"):
        return "backup_out_of_scope"
    if len(rel) >= 2 and rel[1] == ":" and rel[0].isalpha():
        return "backup_out_of_scope"
    parts = rel.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return "backup_out_of_scope"
    if posixpath.normpath(rel) != rel:
        return "backup_out_of_scope"
    return None


def verify_backup(backup: dict, expected_before: str, backup_root: Path | None,
                  live_path: Path | None = None) -> str | None:
    if backup_root is None:
        return "backup_pending"
    rel = backup.get("path") if isinstance(backup, dict) else None
    rel_error = validate_backup_relative_path(rel)
    if rel_error:
        return rel_error

    root = backup_root
    if _chain_reparse(root):
        return "backup_not_independent"
    if not root.exists() or not root.is_dir():
        return "backup_missing"
    root_resolved = root.resolve()
    parts = [p.lower() for p in root_resolved.parts]
    if ".codex" in parts or any(parts[i] == ".gemini" and parts[i + 1] == "config"
                                for i in range(len(parts) - 1)):
        return "backup_not_independent"

    current = root
    for component in rel.split("/"):
        current = current / component
        if _is_reparse(current):
            return "backup_not_independent"
    if not current.is_file():
        return "backup_missing"
    try:
        resolved = current.resolve()
        resolved.relative_to(root_resolved)
    except (ValueError, OSError):
        return "backup_out_of_scope"
    try:
        st = current.stat()
    except OSError:
        return "backup_unreadable"
    if getattr(st, "st_nlink", 1) > 1:
        return "backup_not_independent"
    if live_path is not None:
        try:
            if live_path.exists() and current.samefile(live_path):
                return "backup_not_independent"
        except OSError:
            return "backup_not_independent"
    try:
        if sha256_file(current) != (expected_before or "").lower():
            return "backup_hash_mismatch"
    except OSError:
        return "backup_unreadable"
    return None

def _refuse(code: str, detail: str = "") -> dict:
    return {"ok": False, "ops": [], "errors": [{"code": code, "target": "", "detail": detail}],
            "deployable": False, "status": "not_deployable"}


def _parse_timestamp(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def expected_physical_root() -> Path:
    return Path.home() / ".gemini" / "config"


def _chain_reparse(path: Path) -> bool:
    anchor = Path(path.anchor) if path.anchor else Path(path.parts[0])
    current = anchor
    for part in path.parts[1:]:
        current = current / part
        if _is_reparse(current):
            return True
    return _is_reparse(path)


def plan(manifest: dict, prestate: dict, backup_root: Path | None = None, *,
         now: datetime | None = None, max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
         expected_root: Path | str | None = None) -> dict:
    errors: list[dict] = []
    ops: list[dict] = []
    pending = False

    def err(code: str, target: str = "", detail: str = "") -> None:
        errors.append({"code": code, "target": target, "detail": detail})

    if not isinstance(manifest, dict) or not isinstance(prestate, dict):
        return _refuse("malformed_input")
    actions = manifest.get("actions")
    if not isinstance(actions, list) or not actions:
        return _refuse("empty_deploy_manifest")
    target_root = manifest.get("target_root")
    if target_root != CANONICAL_ROOT:
        return _refuse("manifest_root_mismatch", str(target_root))

    seen: set[str] = set()
    for action in actions:
        kind = action.get("action")
        target = action.get("target")
        digest = (action.get("sha256") or "").lower()
        target_error = validate_target(target)
        if target_error:
            err(target_error, str(target))
            continue
        if target in seen:
            err("duplicate_target", target)
            continue
        seen.add(target)
        if kind not in ALLOWED_ACTIONS:
            err("forbidden_action", target, str(kind))
            continue
        if not is_valid_sha(digest):
            err("malformed_sha256", target)
            continue
        if digest in BLOCKED_HASHES:
            err("blocked_candidate_hash", target)
            continue
        if kind == "rule_replace" and target != RULE_TARGET:
            err("rule_target_mismatch", target)
            continue
        if kind != "rule_replace" and target not in ALLOWED_SKILL_TARGETS:
            err("target_out_of_scope", target)
            continue
        action["_digest"] = digest

    missing_actions = sorted(ALLOWED_TARGETS - seen)
    if missing_actions:
        err("manifest_incomplete", "", f"missing {len(missing_actions)} of {len(ALLOWED_TARGETS)} targets")

    # Positive attestation: booleans alone are not provenance.
    if prestate.get("ok") is not True:
        err("prestate_not_attested", "", "ok must be true")
    if prestate.get("errors") != {}:
        err("prestate_errors_invalid", "", "errors must be an empty object")
    expected = Path(expected_root) if expected_root is not None else expected_physical_root()
    if not expected.exists() or not expected.is_dir() or _chain_reparse(expected):
        err("expected_root_unavailable", "", str(expected))
        physical_root = None
    else:
        physical_root = expected.resolve()
    if prestate.get("root") != str(physical_root) if physical_root is not None else True:
        err("prestate_root_mismatch", "", str(prestate.get("root")))
    generated = _parse_timestamp(prestate.get("generated_at"))
    if generated is None:
        err("prestate_timestamp_missing", "", str(prestate.get("generated_at")))
    else:
        reference = now or datetime.now(timezone.utc)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        age = (reference - generated.astimezone(timezone.utc)).total_seconds()
        if age > max_age_seconds:
            err("prestate_stale", "", f"age {int(age)}s exceeds {max_age_seconds}s")
        elif age < -CLOCK_SKEW_SECONDS:
            err("prestate_timestamp_future", "", f"age {int(age)}s")

    targets = prestate.get("targets")
    if not isinstance(targets, dict):
        return _refuse("missing_prestate")
    if set(targets) != ALLOWED_TARGETS:
        err("prestate_incomplete", "", f"expected {len(ALLOWED_TARGETS)} targets, got {len(targets)}")

    for action in actions:
        target = action.get("target")
        if target not in ALLOWED_TARGETS or target not in targets:
            continue
        digest = action.get("_digest")
        if digest is None:
            continue
        current = targets.get(target)
        if not isinstance(current, dict):
            err("malformed_prestate", target)
            continue
        if current.get("error"):
            err("prestate_error", target, str(current.get("error")))
            continue
        exists = current.get("exists")
        if not isinstance(exists, bool):
            err("malformed_prestate", target, "exists must be bool")
            continue
        current_sha = (current.get("sha256") or "").lower() if current.get("sha256") else None
        if exists and not is_valid_sha(current_sha):
            err("prestate_hash_missing", target)
            continue
        if not exists and current_sha is not None:
            err("malformed_prestate", target, "absent target must have sha256=null")
            continue
        if not exists:
            ops.append({"op": "create", "target": target, "expected_after": digest})
            continue
        if current_sha == digest:
            ops.append({"op": "skip", "target": target, "reason": "already_current"})
            continue
        backup = action.get("backup") or {}
        live_path = None
        if physical_root is not None:
            rel_target = target[len("~/.gemini/config/"):]
            live_path = physical_root / rel_target
        backup_error = verify_backup(backup, current_sha, backup_root, live_path)
        if backup_error == "backup_pending":
            pending = True
            err("backup_pending", target)
            continue
        if backup_error:
            err(backup_error, target)
            continue
        ops.append({"op": "replace", "target": target, "backup": backup.get("path"),
                    "expected_before": current_sha, "expected_after": digest})

    deployable = not errors and not pending
    return {"ok": deployable, "ops": ops, "errors": errors, "deployable": deployable,
            "status": "dry_run_ok" if deployable else "not_deployable",
            "counts": {"create": sum(1 for o in ops if o["op"] == "create"),
                       "replace": sum(1 for o in ops if o["op"] == "replace"),
                       "skip": sum(1 for o in ops if o["op"] == "skip")}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--prestate", type=Path, required=True)
    ap.add_argument("--backup-root", type=Path,
                    help="independent backup root; without it every replacement stays backup_pending")
    ap.add_argument("--output", type=Path)
    args = ap.parse_args(argv)
    result = plan(json.loads(args.manifest.read_text(encoding="utf-8-sig")),
                  json.loads(args.prestate.read_text(encoding="utf-8-sig")),
                  args.backup_root)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())


