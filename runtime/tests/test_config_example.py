"""Keep the tracked example aligned with parameters the code actually reads.

The matching table is in runtime/README.md. ``profile_path`` was historically
present but is not read: chat.py always uses ``runtime/profile``. The key was
removed from the example; ``test_env.py`` also pops it defensively.
"""
import unittest
from pathlib import Path

import yaml

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.yaml"

PHONE_KEYS = {"serial", "adb_path", "package", "view_width", "send_width", "pace_seconds"}
BROWSER_KEYS = {"chrome_path", "cdp_port", "mode", "reply_timeout_seconds", "stable_seconds"}
PROVIDER_KEYS = {
    "title", "host", "url", "editor", "send", "stop", "assistant",
    "assistant_text", "file_input", "upload_clicks", "attach_methods",
}


class ConfigExampleTests(unittest.TestCase):
    def setUp(self):
        self.cfg = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))

    def test_no_unused_profile_path(self):
        self.assertNotIn("profile_path", self.cfg)

    def test_phone_and_browser_keys_are_used(self):
        self.assertEqual(set(self.cfg["phone"]), PHONE_KEYS)
        self.assertEqual(set(self.cfg["browser"]), BROWSER_KEYS)

    def test_chat_keys_are_used(self):
        chats = self.cfg["chats"]
        self.assertIn(chats["primary"], ("gemini", "chatgpt"))
        for provider in ("gemini", "chatgpt"):
            self.assertEqual(set(chats[provider]), PROVIDER_KEYS, provider)


if __name__ == "__main__":
    unittest.main()