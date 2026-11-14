#!/usr/bin/env python3
"""Read-only preflight of the live Antigravity PDD configuration.

Reads only public metadata: existence, type, size, SHA-256, reparse flags,
structural booleans of the global rule, and process counts. Never prints or
exports file contents, command lines, profiles, cookies, tokens or addresses.
Writes results only inside the staging tree (preflight/).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

STAGING = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGING / "ops"))
from transaction_plan import ALLOWED_TARGETS, RULE_TARGET  # noqa: E402

HOME = Path(os.environ.get("USERPROFILE") or Path.home())
CONFIG = HOME / ".gemini" / "config"
RULE = HOME / ".gemini" / "config" / "rules" / "AGENTS.md"
PDD_PY = HOME / ".codex" / "pdd-operator" / "pdd.py"
EXPECTED_PDD = "817a2a97a743c27eb9aa64f99d0c7eeb479f19e3ea6bfe7d370d154d712e8af6"

REQUIRED_CONTROLS = ["立即支付", "提交订单", "确认支付", "去结算", "直接拼成", "全选",
                     "tap --force", "收藏", "не равна нулю", "Жесты: запрет до приёмки",
                     "skill_discovery_unverified"]
STALE_TOKENS = ["Gemini 3.8", "scratch\\pdd-operator", "все жесты защищены",
                "--operator-dir", "PDD_OPERATOR_DIR"]
UNRELATED = re.compile(r"^#{1,6}\s+.*(заметк|личн|общ|git|python|проект|security|system|unrelated)", re.I)


def is_reparse(path: Path) -> bool:
    try:
        st = path.lstat()
    except OSError:
        return False
    return bool(stat.S_ISLNK(st.st_mode)) or bool(
        getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def reparse_in_path(path: Path) -> bool:
    """True if the path itself or any ancestor is a symlink/junction/reparse."""
    current = path
    while True:
        if is_reparse(current):
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def sha256(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def path_entry(path: Path) -> dict:
    entry = {"exists": path.exists(), "reparse": reparse_in_path(path)}
    if not entry["exists"]:
        return entry
    try:
        st = path.stat()
        entry["is_file"] = path.is_file()
        entry["size"] = st.st_size
    except OSError:
        entry["error"] = "stat_failed"
        return entry
    if entry.get("is_file"):
        entry["sha256"] = sha256(path)
    return entry


def target_path(target: str) -> Path:
    prefix = "~/.gemini/config/"
    rel = target[len(prefix):] if target.startswith(prefix) else target
    return CONFIG / rel


def rule_structure() -> dict:
    if not RULE.is_file():
        return {"exists": False}
    try:
        text = RULE.read_text(encoding="utf-8-sig", errors="ignore")
    except OSError:
        return {"exists": True, "readable": False}
    h1 = [l for l in text.splitlines() if re.match(r"^#\s+\S", l)]
    h2 = [l for l in text.splitlines() if re.match(r"^##+\s+\S", l)]
    return {
        "exists": True,
        "readable": True,
        "sha256": sha256(RULE),
        "size": RULE.stat().st_size,
        "h1_count": len(h1),
        "h1_is_pdd": bool(h1) and bool(re.search(r"Pinduoduo|拼多多|PDD", h1[0])),
        "h2_count": len(h2),
        "unrelated_sections": [bool(UNRELATED.match(line)) for line in h2 if UNRELATED.match(line)],
        "pdd_only": len(h1) == 1 and bool(h1) and bool(re.search(r"Pinduoduo|拼多多|PDD", h1[0]))
                    and not any(UNRELATED.match(line) for line in text.splitlines()),
        "required_controls": {c: (c in text) for c in REQUIRED_CONTROLS},
        "stale_markers": {t: (t in text) for t in STALE_TOKENS},
        "has_bom": RULE.read_bytes()[:3] == b"\xef\xbb\xbf",
    }


def process_counts() -> dict:
    """Counts only; command lines are inspected locally and never exported."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | Select-Object Name,CommandLine | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=25, check=False)
        rows = json.loads(out.stdout or "[]")
        if isinstance(rows, dict):
            rows = [rows]
    except Exception:
        return {"available": False}
    counts = {"pdd_owners": 0, "chat_owners": 0, "chrome": 0, "chrome_cdp": 0, "adb": 0}
    for row in rows:
        name = (row.get("Name") or "").lower()
        cmd = row.get("CommandLine") or ""
        if name.startswith("python") and "pdd.py" in cmd:
            counts["pdd_owners"] += 1
        if name.startswith("python") and "chat.py" in cmd:
            counts["chat_owners"] += 1
        if name == "chrome.exe":
            counts["chrome"] += 1
            if "remote-debugging" in cmd:
                counts["chrome_cdp"] += 1
        if name == "adb.exe":
            counts["adb"] += 1
    counts["available"] = True
    return counts


def antigravity() -> dict:
    exe_candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Antigravity" / "Antigravity.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "antigravity" / "Antigravity.exe",
    ]
    return {"installed_hint": any(p.exists() for p in exe_candidates),
            "version": "not_verified", "workspace": "not_verified"}


def main() -> int:
    targets = sorted(ALLOWED_TARGETS)
    entries = {t: path_entry(target_path(t)) for t in targets}
    rule = rule_structure()
    pdd = {"path_known": True, "exists": PDD_PY.is_file(), "sha256": sha256(PDD_PY) if PDD_PY.is_file() else None}
    pdd["matches_expected_baseline"] = pdd["sha256"] == EXPECTED_PDD
    procs = process_counts()
    fixture = STAGING / "rule-migration" / "old-rule.redacted.md"
    fixture_sha = sha256(fixture) if fixture.is_file() else None
    rule["matches_reviewed_fixture"] = bool(rule.get("sha256") and fixture_sha and rule["sha256"] == fixture_sha)

    blocking = []
    if is_reparse(CONFIG):
        blocking.append("config_root_reparse")
    for t, e in entries.items():
        if e.get("reparse"):
            blocking.append(f"reparse:{t}")
        if e.get("error"):
            blocking.append(f"error:{t}:{e['error']}")
        if not e.get("exists"):
            blocking.append(f"absent:{t}")
    if rule.get("exists") and not rule.get("pdd_only", False):
        blocking.append("rule_not_pdd_only")
    if rule.get("exists") and rule.get("unrelated_sections"):
        blocking.append("rule_has_unrelated_sections")
    if procs.get("pdd_owners", 0) > 0 or procs.get("chat_owners", 0) > 0:
        blocking.append("possible_competing_operator_session")
    if procs.get("adb", 0) > 0:
        blocking.append("adb_process_present")
    if not pdd["matches_expected_baseline"]:
        blocking.append("pdd_py_hash_differs_from_baseline")

    summary = {
        "generated": "2026-11-13",
        "mode": "read_only_metadata_only",
        "config_root": {"exists": CONFIG.exists(), "reparse": is_reparse(CONFIG)},
        "targets_total": len(targets),
        "targets_present": sum(1 for e in entries.values() if e.get("exists")),
        "targets_absent": sum(1 for e in entries.values() if not e.get("exists")),
        "targets_reparse": sum(1 for e in entries.values() if e.get("reparse")),
        "targets": entries,
        "rule": rule,
        "pdd_py": pdd,
        "processes": procs,
        "antigravity": antigravity(),
        "blocking": blocking,
        "verdict": "not_deployable",
        "reasons": ["live backup not created", "user authorization not granted"],
        "statuses": {"skill_discovery_unverified": True, "concurrency_unknown": True,
                     "source_not_yet_verified": True, "runtime_scroll": "BLOCKED",
                     "cmd_swipe_risk": "open"},
    }
    out_dir = STAGING / "preflight"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "PRESTATE-SUMMARY.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "targets": len(targets),
                      "present": summary["targets_present"],
                      "blocking": blocking,
                      "output": str(out_dir / "PRESTATE-SUMMARY.json")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

