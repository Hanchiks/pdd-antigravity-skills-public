#!/usr/bin/env python3
"""Deterministic, fail-closed migration of a PDD-only global rule.

Safety model:
- a full-file replacement is allowed ONLY for an exact known source fingerprint
  (SHA-256 of the current file content), never on a loose "contains Pinduoduo"
  heuristic;
- unrelated H1/H2 sections and unmarked unrelated paragraphs change the
  fingerprint, so they are rejected instead of silently discarded;
- output always goes to a NEW candidate path; this tool never writes live config.

The redacted fixture in this package cannot prove what the LIVE rule contains.
For a live migration the operator must pass the freshly computed live hash via
--approved-sha256 after an explicit review, and keep a recoverable backup.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

# SHA-256 of rule-migration/old-rule.redacted.md (the reviewed fixture).
KNOWN_SOURCE_SHA256 = {
    "e52f444760701972c88bfd8155d2483d6b6cdb4481982965bcf7ea99494d3d36",
}

STALE_TOKENS = [
    "Gemini 3.8",
    "scratch\\pdd-operator",
    "все жесты защищены",
    "--operator-dir",
    "PDD_OPERATOR_DIR",
]
NEGATIONS = ("не существует", "не создавать", "не подменять", "запрещ", "не является", "не должно")

REQUIRED = [
    "直接拼成",
    "全选",
    "tap --force",
    "не равна нулю",
    "Жесты: запрет до приёмки",
    "skill_discovery_unverified",
]

UNRELATED_SECTION = re.compile(r"^#{2,6}\s+.*(заметк|личн|общ|git|python|проект|security|system|unrelated)", re.I)


class RuleMergeError(RuntimeError):
    pass


def is_live_config_path(path: Path) -> bool:
    """True for live Antigravity/Codex configuration locations."""
    parts = [part.lower() for part in path.resolve().parts]
    if ".codex" in parts:
        return True
    return any(parts[i] == ".gemini" and parts[i + 1] == "config" for i in range(len(parts) - 1))


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stale_claim_lines(text: str) -> list[str]:
    bad = []
    for line in text.splitlines():
        for sentence in re.split(r"(?<=[.;!?])\s+", line):
            if any(token in sentence for token in STALE_TOKENS):
                low = sentence.lower()
                if not any(neg in low for neg in NEGATIONS):
                    bad.append(sentence.strip())
    return bad


def is_pdd_only(text: str) -> bool:
    """Structural pre-check; the exact fingerprint remains the primary gate."""
    h1 = [line for line in text.splitlines() if re.match(r"^#\s+\S", line)]
    if len(h1) != 1:
        return False
    if not re.search(r"Pinduoduo|拼多多|PDD", h1[0]):
        return False
    return not any(UNRELATED_SECTION.match(line) for line in text.splitlines())


def migrate(current: str, target: str, approved_hashes: set[str] | None = None) -> str:
    approved = KNOWN_SOURCE_SHA256 if approved_hashes is None else set(approved_hashes)
    digest = fingerprint(current)
    if digest not in approved:
        raise RuleMergeError(
            "rule_merge_unverified: неизвестный или изменённый источник "
            f"(sha256={digest}); посторонние правила не удаляются автоматически")
    if not is_pdd_only(current):
        raise RuleMergeError("rule_merge_unverified: файл не является чисто PDD-правилом")
    for token in REQUIRED:
        if token not in target:
            raise RuleMergeError(f"rule_merge_unverified: в целевом правиле нет обязательного элемента {token!r}")
    new_text = target if target.endswith("\n") else target + "\n"
    stale = stale_claim_lines(new_text)
    if stale:
        raise RuleMergeError(f"rule_merge_unverified: устаревшее утверждение в целевом правиле: {stale[0]!r}")
    if new_text.count("# PDD rules — portable overlay") != 1:
        raise RuleMergeError("rule_merge_unverified: PDD-блок в результате должен встречаться ровно один раз")
    return new_text


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--current", type=Path, required=True)
    ap.add_argument("--target", type=Path, required=True, help="reviewed overlay text (PDD-SECTION.md)")
    ap.add_argument("--output", type=Path, required=True, help="candidate path; must differ from current")
    ap.add_argument("--approved-sha256", action="append", default=[], metavar="HEX",
                    help="explicitly approved current-file hash; repeatable. Required for a live rule.")
    ap.add_argument("--staging-root", type=Path, default=Path(__file__).resolve().parents[1],
                    help="output must stay inside this staging tree (default: package root)")
    args = ap.parse_args(argv)
    try:
        if args.output.resolve() == args.current.resolve():
            raise RuleMergeError("отказ: выходной путь совпадает с текущим правилом")
        if is_live_config_path(args.output):
            raise RuleMergeError("отказ: запись в живой каталог конфигурации запрещена")
        if not is_within(args.output, args.staging_root):
            raise RuleMergeError("отказ: выходной путь вне выбранного staging-каталога")
        approved = set(args.approved_sha256) | (KNOWN_SOURCE_SHA256 if not args.approved_sha256 else set())
        result = migrate(args.current.read_text(encoding="utf-8-sig"),
                         args.target.read_text(encoding="utf-8-sig"), approved)
    except RuleMergeError as exc:
        print(json.dumps({"ok": False, "code": "rule_merge_unverified", "detail": str(exc)}, ensure_ascii=False))
        return 2
    args.output.write_text(result, encoding="utf-8", newline="\n")
    print(json.dumps({"ok": True, "output": str(args.output), "bytes": len(result.encode("utf-8")),
                      "source_sha256": fingerprint(args.current.read_text(encoding="utf-8-sig"))},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())



