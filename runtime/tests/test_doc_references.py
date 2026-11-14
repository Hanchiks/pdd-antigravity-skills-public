"""Check relative documentation links and shipped skill assets.

Only local files are checked; external URLs are deliberately not requested.
``experimental/02ag1a4/`` is excluded because it is a frozen historical
snapshot with status ``not_deployable``: its README and links must not be
edited or treated as current documentation.
"""
import json
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
EXCLUDED = (REPO / "experimental" / "02ag1a4").resolve()
SKILL = REPO / "skills" / "pinduoduo-buyer"

LINK_RE = re.compile(r"(?<!!)\[[^\]\n]*\]\(([^)\n]+)\)")
CODE_RE = re.compile(r"`([^`\n]+)`")
ASSET_REF_RE = re.compile(r"^(?:assets|references|scripts)/[^\s`*?<>]+\.(?:json|md|py|txt|ya?ml)$")
INTENTIONALLY_ABSENT = {"assets/user-profile.private.json"}


def markdown_files():
    for path in sorted(REPO.rglob("*.md")):
        if ".git" in path.parts or EXCLUDED in path.parents:
            continue
        yield path


def relative_link_target(raw):
    target = raw.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1].strip()
    if not target or target.startswith(("#", "http://", "https://", "mailto:", "~", "/")):
        return None
    target = target.split()[0].strip("\"'")
    target = target.split("#", 1)[0]
    if not target or "{" in target or "}" in target:
        return None
    return target


class DocumentationLinkTests(unittest.TestCase):
    def test_relative_markdown_links_resolve(self):
        broken = []
        for path in markdown_files():
            text = path.read_text(encoding="utf-8")
            for match in LINK_RE.finditer(text):
                target = relative_link_target(match.group(1))
                if target is None:
                    continue
                resolved = (path.parent / target).resolve()
                if not resolved.exists():
                    broken.append(f"{path.relative_to(REPO)} -> {target}")
        self.assertEqual(broken, [], "broken relative markdown links:\n" + "\n".join(broken))

    def test_skill_code_span_asset_references_exist(self):
        broken = []
        for path in sorted(SKILL.rglob("*.md")):
            text = path.read_text(encoding="utf-8")
            for match in CODE_RE.finditer(text):
                target = match.group(1).strip()
                if target in INTENTIONALLY_ABSENT:
                    continue
                if not ASSET_REF_RE.fullmatch(target):
                    continue
                candidates = (path.parent / target, SKILL / target)
                if not any(candidate.exists() for candidate in candidates):
                    broken.append(f"{path.relative_to(REPO)} -> {target}")
        self.assertEqual(broken, [], "missing referenced skill files:\n" + "\n".join(broken))

    def test_synthetic_price_example_is_present_and_labeled(self):
        path = SKILL / "assets" / "synthetic-price-example.json"
        self.assertTrue(path.is_file())
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIs(data.get("synthetic"), True)
        self.assertTrue(data.get("candidates"))


if __name__ == "__main__":
    unittest.main()