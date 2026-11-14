import os
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import pdd


class CleanTests(unittest.TestCase):
    def test_stale_files_respect_cutoff_and_ignore_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "old.jpg"
            new = root / "new.jpg"
            old.write_bytes(b"old")
            new.write_bytes(b"new")
            old_time = time.time() - 10 * 86400
            os.utime(old, (old_time, old_time))
            stale = pdd._stale_files(root, time.time() - 7 * 86400)
            self.assertEqual(stale, [old])

    def test_missing_directory_is_empty(self):
        self.assertEqual(pdd._stale_files(Path(tempfile.gettempdir()) / "no-such-pdd-dir", time.time()), [])

    def test_clean_rejects_non_positive_days(self):
        args = type("A", (), {"days": 0, "apply": False})()
        with self.assertRaises(pdd.AdbError):
            pdd.cmd_clean(args)


class CleanReportTests(unittest.TestCase):
    def _age(self, path, days=10):
        stamp = time.time() - days * 86400
        os.utime(path, (stamp, stamp))

    def _tree(self, root):
        logs = root / "logs"
        (logs / "screens").mkdir(parents=True)
        (logs / "chats").mkdir(parents=True)
        old_screen = logs / "screens" / "old.jpg"
        new_screen = logs / "screens" / "new.jpg"
        old_chat = logs / "chats" / "old.txt"
        old_screen.write_bytes(b"old")
        new_screen.write_bytes(b"new")
        old_chat.write_bytes(b"chat")
        self._age(old_screen)
        self._age(old_chat)
        other = logs / "other" / "keep.txt"
        other.parent.mkdir(parents=True)
        other.write_bytes(b"other")
        self._age(other)
        profile = root / "profile" / "cache.bin"
        profile.parent.mkdir(parents=True)
        profile.write_bytes(b"profile")
        tools = root / "tools" / "ADBKeyboard.apk"
        tools.parent.mkdir(parents=True)
        tools.write_bytes(b"apk")
        state = root / "state.json"
        state.write_bytes(b"state")
        self._age(profile)
        self._age(tools)
        self._age(state)
        return old_screen, new_screen, old_chat, profile, tools, state

    def _run(self, root, apply):
        with patch.object(pdd, "ROOT", root), patch.object(pdd, "out") as output:
            pdd.cmd_clean(types.SimpleNamespace(days=7, apply=apply))
        return output.call_args.args[0]

    def test_dry_run_reports_would_delete_without_touching_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_screen, new_screen, old_chat, profile, tools, state = self._tree(root)
            result = self._run(root, apply=False)
            self.assertEqual(result["status"], "dry_run")
            self.assertTrue(result["ok"])
            self.assertEqual(result["found"], 2)
            self.assertEqual(result["deleted"], 0)
            self.assertEqual(set(result["would_delete"]), {str(old_screen), str(old_chat)})
            for path in (old_screen, new_screen, old_chat, profile, tools, state):
                self.assertTrue(path.exists(), path)
            self.assertTrue((root / "logs" / "other" / "keep.txt").exists())

    def test_apply_deletes_only_allowed_stale_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_screen, new_screen, old_chat, profile, tools, state = self._tree(root)
            result = self._run(root, apply=True)
            self.assertEqual(result["status"], "ok")
            self.assertTrue(result["ok"])
            self.assertEqual(result["deleted"], 2)
            self.assertEqual(set(result["deleted_paths"]), {str(old_screen), str(old_chat)})
            self.assertFalse(old_screen.exists())
            self.assertFalse(old_chat.exists())
            for path in (new_screen, profile, tools, state):
                self.assertTrue(path.exists(), path)
            self.assertTrue((root / "logs" / "other" / "keep.txt").exists())

    def test_partial_failure_is_reported_and_others_still_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            screens = root / "logs" / "screens"
            screens.mkdir(parents=True)
            bad = screens / "bad.jpg"
            good = screens / "good.jpg"
            bad.write_bytes(b"bad")
            good.write_bytes(b"good")
            self._age(bad)
            self._age(good)
            real_unlink = Path.unlink

            def fake_unlink(self, *args, **kwargs):
                if self == bad:
                    raise OSError(13, "denied")
                return real_unlink(self, *args, **kwargs)

            with patch.object(Path, "unlink", fake_unlink):
                result = self._run(root, apply=True)
            self.assertEqual(result["status"], "partial")
            self.assertFalse(result["ok"])
            self.assertEqual(result["deleted"], 1)
            self.assertEqual(result["failed"], 1)
            self.assertEqual(result["errors"][0]["path"], str(bad))
            self.assertIn("denied", result["errors"][0]["error"])
            self.assertFalse(good.exists())
            self.assertTrue(bad.exists())

    def test_symlinks_are_skipped_and_outside_targets_are_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside"
            outside.mkdir()
            outside_file = outside / "secret.txt"
            outside_file.write_bytes(b"secret")
            self._age(outside_file)
            screens = root / "logs" / "screens"
            screens.mkdir(parents=True)
            stale = screens / "old.jpg"
            stale.write_bytes(b"old")
            self._age(stale)
            try:
                os.symlink(outside_file, screens / "file_link.jpg")
                os.symlink(outside, screens / "dir_link")
            except OSError as exc:
                self.skipTest(f"символические ссылки недоступны: {exc}")
            result = self._run(root, apply=True)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["deleted_paths"], [str(stale)])
            self.assertFalse(stale.exists())
            self.assertTrue(outside_file.exists())
            self.assertTrue((screens / "file_link.jpg").is_symlink())
            self.assertTrue((screens / "dir_link").is_symlink())

    def test_symlinked_log_root_is_not_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside"
            outside.mkdir()
            secret = outside / "secret.jpg"
            secret.write_bytes(b"secret")
            self._age(secret)
            logs = root / "logs"
            logs.mkdir()
            try:
                os.symlink(outside, logs / "screens")
            except OSError as exc:
                self.skipTest(f"символические ссылки недоступны: {exc}")
            result = self._run(root, apply=True)
            self.assertEqual(result["found"], 0)
            self.assertTrue(secret.exists())

    def test_files_outside_documented_clean_scope_are_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / "logs"
            logs.mkdir(parents=True)
            push_photo = logs / "pdd_search_ref_old.jpg"
            inspect = root / "work" / "inspect.png"
            inspect.parent.mkdir(parents=True)
            push_photo.write_bytes(b"photo")
            inspect.write_bytes(b"inspect")
            self._age(push_photo)
            self._age(inspect)
            result = self._run(root, apply=True)
            self.assertEqual(result["found"], 0)
            self.assertEqual(result["deleted"], 0)
            self.assertTrue(push_photo.exists())
            self.assertTrue(inspect.exists())

    def test_symlinked_logs_directory_is_not_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside"
            (outside / "screens").mkdir(parents=True)
            secret = outside / "screens" / "secret.jpg"
            secret.write_bytes(b"secret")
            self._age(secret)
            try:
                os.symlink(outside, root / "logs")
            except OSError as exc:
                self.skipTest(f"символические ссылки недоступны: {exc}")
            result = self._run(root, apply=True)
            self.assertEqual(result["found"], 0)
            self.assertTrue(secret.exists())


if __name__ == "__main__":
    unittest.main()