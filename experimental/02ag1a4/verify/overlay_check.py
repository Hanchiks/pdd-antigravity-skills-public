#!/usr/bin/env python3
"""Verifier for the 02AG1P1 Antigravity staging package (read-only).

Semantic checks beyond token presence:
- false scratch runtime claim (unless explicitly negated);
- wrong buyer-skill path / missing canonical runtime path;
- missing explicit scroll/swipe prohibition;
- merged rule candidate differing from the reviewed overlay;
- an explicit allowance to press the purchase button for price;
- deploy manifest targeting unchanged originals instead of adapted overlay files;
- missing/unverified backup for a planned overwrite.
"""
from __future__ import annotations

import hashlib
import json
import re
import stat
from pathlib import Path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def is_reparse(path: Path) -> bool:
    try:
        st = path.lstat()
    except OSError:
        return False
    return bool(stat.S_ISLNK(st.st_mode)) or bool(
        getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def stale_claims(text: str, tokens, negations) -> list[str]:
    """A stale marker is a violation unless its own sentence negates it."""
    out = []
    for line in text.splitlines():
        for sentence in re.split(r"(?<=[.;!?])\s+", line):
            if any(t in sentence for t in tokens):
                low = sentence.lower()
                if not any(n in low for n in negations):
                    out.append(sentence.strip())
    return out


def strict_frontmatter(path: Path, required=("name", "description")):
    """Strict discovery-compatible check: file must start with ASCII '---\\n'."""
    data = path.read_bytes()
    if not data.startswith(b"---\n"):
        return None, "no_leading_frontmatter"
    end = data.find(b"\n---", 4)
    if end < 0:
        return None, "unterminated_frontmatter"
    block = data[4:end].decode("utf-8", "replace")
    values = {}
    for line in block.splitlines():
        match = re.match(r"^([A-Za-z_][\w-]*):\s*(.+)$", line)
        if match:
            values[match.group(1)] = match.group(2).strip()
    for key in required:
        if not values.get(key):
            return None, f"missing_{key}"
    return values, None


def press_allowances(text: str, policy: dict, window: int = 180) -> list[str]:
    """Flag explicit permission to press the purchase button for price.

    Works on a flattened stream so line-wrapped instructions cannot evade the
    check, but only within a bounded window around 直接拼成. A real prohibition
    in that window waives the finding; "не запрещено" is permission, not
    prohibition, and is not in the prohibit pattern.
    """
    permission = re.compile(policy["press_permission"], re.I)
    price = re.compile(policy["press_price"], re.I)
    prohibit = re.compile(policy["press_prohibit"], re.I)
    flat = " ".join(text.split())
    out: list[str] = []
    for match in re.finditer("直接拼成", flat):
        start = max(0, match.start() - window)
        stop = min(len(flat), match.end() + window)
        fragment = flat[start:stop]
        if prohibit.search(fragment):
            continue
        if permission.search(fragment) and price.search(fragment):
            snippet = fragment.strip()
            if snippet not in out:
                out.append(snippet)
    return out


def check(root: Path) -> dict:
    errors: list[dict] = []
    warnings: list[dict] = []
    policy = json.loads((root / "verify" / "policy.json").read_text(encoding="utf-8-sig"))

    def err(code: str, path: str = "", detail: str = "") -> None:
        errors.append({"code": code, "path": path, "detail": detail})

    orig = root / policy["original_root"]
    overlay = root / policy["overlay_root"]
    if not orig.is_dir() or not overlay.is_dir():
        return {"ok": False, "errors": [{"code": "missing_tree", "path": "", "detail": "ORIGINAL/OVERLAY"}],
                "warnings": []}

    orig_rel = {p.relative_to(orig).as_posix(): p for p in orig.rglob("*") if p.is_file()}
    overlay_rel = {p.relative_to(overlay).as_posix(): p for p in overlay.rglob("*") if p.is_file()}

    if len(orig_rel) != policy["original_expected_count"]:
        err("original_count", policy["original_root"], f"{len(orig_rel)} != {policy['original_expected_count']}")

    allowed = set(policy["allowed_adaptations"])
    for inner, path in sorted(orig_rel.items()):
        key = f"skills/{inner}"
        target = overlay_rel.get(inner)
        if target is None:
            err("missing_overlay_file", key)
            continue
        if key not in allowed and sha256(target) != sha256(path):
            err("unexpected_adaptation", key)
        if sha256(path) in policy["blocked_candidate_hashes"]:
            err("blocked_candidate", key)

    for inner in sorted(overlay_rel):
        if inner not in orig_rel:
            err("extra_overlay_file", f"skills/{inner}")

    # ---- operator skill: paths and fail-closed gestures --------------
    op_inner = "pdd-operator/SKILL.md"
    op = overlay_rel.get(op_inner)
    if op is not None:
        text = op.read_text(encoding="utf-8-sig")
        if sha256(op) == sha256(orig_rel[op_inner]):
            err("operator_not_adapted", f"skills/{op_inner}")
        stale = stale_claims(text, policy["stale_tokens"], policy["negations"])
        if stale:
            err("false_scratch_runtime_path", f"skills/{op_inner}", stale[0])
        if policy["required_buyer_path"] not in text:
            err("wrong_buyer_skill_path", f"skills/{op_inner}")
        if policy["required_runtime_path"] not in text:
            err("missing_runtime_path", f"skills/{op_inner}")
        for phrase in policy["gesture_phrases"]:
            if phrase not in text:
                err("missing_gesture_prohibition", f"skills/{op_inner}", phrase)
        for line in press_allowances(text, policy):
            err("allows_purchase_button_for_price", f"skills/{op_inner}", line)

    # ---- buyer skill: private profile must be conditional -----------
    bp = overlay_rel.get("pinduoduo-buyer/SKILL.md")
    if bp is not None:
        btext = bp.read_text(encoding="utf-8-sig")
        if "user-profile.private.json" in btext and "если пользователь разрешил" not in btext:
            err("private_profile_unconditional", "skills/pinduoduo-buyer/SKILL.md")

    # ---- adapted skills: strict frontmatter + semantic prohibitions --
    for inner in ("pdd-operator/SKILL.md", "pinduoduo-buyer/SKILL.md"):
        skill_path = overlay_rel.get(inner)
        if skill_path is None:
            continue
        values, why = strict_frontmatter(skill_path, tuple(policy["frontmatter_required"]))
        if why:
            err("frontmatter_invalid", f"skills/{inner}", why)
        elif values.get("name") != inner.split("/")[0]:
            err("frontmatter_name_mismatch", f"skills/{inner}", values.get("name", ""))
        for line in press_allowances(skill_path.read_text(encoding="utf-8-sig"), policy):
            err("allows_purchase_button_for_price", f"skills/{inner}", line)

    # ---- overlay rule ----------------------------------------------
    rule = root / policy["rule_file"]
    if not rule.is_file():
        err("missing_rule", policy["rule_file"])
    else:
        text = rule.read_text(encoding="utf-8-sig")
        for control in policy["required_controls"]:
            if control not in text:
                err("control_missing", policy["rule_file"], control)
        for phrase in policy["gesture_phrases"]:
            if phrase not in text:
                err("missing_gesture_prohibition", policy["rule_file"], phrase)
        for line in stale_claims(text, policy["stale_tokens"], policy["negations"]):
            err("stale_token", policy["rule_file"], line)
        for line in press_allowances(text, policy):
            err("allows_purchase_button_for_price", policy["rule_file"], line)

    # ---- merged rule candidate -------------------------------------
    merged = root / policy["merged_candidate"]
    if not merged.is_file():
        err("missing_merged_candidate", policy["merged_candidate"])
    elif rule.is_file():
        if sha256(merged) != sha256(rule):
            err("merged_candidate_conflict", policy["merged_candidate"], "differs from reviewed rule")
        mtext = merged.read_text(encoding="utf-8-sig")
        for line in stale_claims(mtext, policy["stale_tokens"], policy["negations"]):
            err("stale_token", policy["merged_candidate"], line)
        if mtext.count("# PDD rules — portable overlay") != 1:
            err("merged_candidate_conflict", policy["merged_candidate"], "PDD block count != 1")

    # ---- deploy manifest + prestate --------------------------------
    man_path = root / policy["deploy_manifest"]
    pre_path = root / policy["prestate"]
    if not man_path.is_file():
        err("missing_deploy_manifest", policy["deploy_manifest"])
    else:
        man = json.loads(man_path.read_text(encoding="utf-8-sig"))
        actions = man.get("actions", [])
        if not actions:
            err("empty_deploy_manifest", policy["deploy_manifest"])
        for action in actions:
            kind = action.get("action")
            target = action.get("target", "")
            digest = (action.get("sha256") or "").lower()
            if kind not in {"create", "replace", "rule_replace"}:
                err("forbidden_manifest_action", target, str(kind))
            if digest in policy["blocked_candidate_hashes"]:
                err("blocked_candidate", target)
            if kind == "rule_replace":
                backup = action.get("backup") or {}
                backup_sha = backup.get("sha256") or ""
                if not backup.get("path") or not (len(backup_sha) == 64 and all(
                        c in "0123456789abcdefABCDEF" for c in backup_sha)):
                    err("backup_not_required", target)
                if rule.is_file() and sha256(rule) != digest:
                    err("manifest_target_mismatch", target, "rule hash differs from manifest")
                continue
            prefix = "~/.gemini/config/skills/"
            if not target.startswith(prefix):
                err("manifest_target_outside_skills", target)
                continue
            rel = target[len(prefix):]
            exported = overlay_rel.get(rel)
            if exported is None:
                err("manifest_target_missing", target)
                continue
            if sha256(exported) != digest:
                err("manifest_target_mismatch", target)
            original = orig_rel.get(rel)
            if (original is not None and sha256(exported) != sha256(original)
                    and digest == sha256(original)):
                err("manifest_targets_original", target)

    if not pre_path.is_file():
        err("missing_prestate", policy["prestate"])

    # ---- inventory hygiene ----------------------------------------
    for path in root.rglob("*"):
        rel = path.relative_to(root).as_posix()
        if rel.startswith("checks/"):
            continue
        if is_reparse(path):
            err("reparse_point", rel)
        if not path.is_file():
            continue
        if any(part in policy["forbidden_dirs"] for part in rel.split("/")[:-1]):
            err("forbidden_path", rel, "forbidden directory")
        if path.name in policy["forbidden_names"] or any(
                path.name.endswith(suffix) for suffix in policy["forbidden_suffixes"]):
            err("forbidden_path", rel, "forbidden file")
        if path.name in policy["private_markers"]:
            err("private_marker", rel)
        if sha256(path) in policy["blocked_candidate_hashes"]:
            err("blocked_candidate", rel)
        if rel.startswith("verify/"):
            continue
        if path.suffix in {".md", ".py", ".json", ".yaml", ".txt"}:
            text = path.read_text(encoding="utf-8-sig", errors="ignore")
            for pattern in policy["pii_patterns"]:
                if re.search(pattern, text):
                    err("pii_pattern", rel, pattern)

    return {"ok": not errors, "errors": errors, "warnings": warnings,
            "counts": {"errors": len(errors), "warnings": len(warnings), "files": len(orig_rel)}}


def main(argv=None) -> int:
    root = Path(argv[0]) if argv else Path(__file__).resolve().parents[1]
    result = check(root.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())





