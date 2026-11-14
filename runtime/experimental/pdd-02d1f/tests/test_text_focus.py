import base64
import json
import subprocess
import types
import unittest
from unittest.mock import patch

import pdd


SC = {"real_w": 1080, "real_h": 2400, "view_w": 720, "view_h": 1600, "scale": 1.5}


def tree(*, rid="search_input", focused="true", editable="true", enabled="true",
         package=None, extra="", bounds="[90,150][900,240]", text=""):
    package = pdd.PH["package"] if package is None else package
    return pdd.UiDump(f'<hierarchy><node package="{package}" class="android.widget.EditText" '
            f'resource-id="{rid}" focused="{focused}" editable="{editable}" '
            f'enabled="{enabled}" bounds="{bounds}" text="{text}" />{extra}</hierarchy>')


class TextFocusTests(unittest.TestCase):
    def setUp(self):
        for name, value in (("ensure_device", None), ("_require_pdd_focus", None),
                            ("screen_info", SC), ("out", None), ("pace", None)):
            patcher = patch.object(pdd, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        serial = patch.object(pdd, "SERIAL", "test-device")
        serial.start()
        self.addCleanup(serial.stop)
        patcher = patch.object(pdd.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_only_focused_enabled_pdd_editable_is_accepted(self):
        cases = [tree(focused="false"), tree(enabled="false"), tree(package="other.app"),
                 tree(bounds="[0,0][0,0]"), tree(extra='<node focused="true" />'),
                 pdd.UiDump('<hierarchy><node focused="true" package="other.app" class="android.widget.Button" enabled="true" bounds="[10,100][80,150]" /></hierarchy>')]
        with patch.object(pdd, "dump_ui_xml", return_value=tree()):
            self.assertTrue(pdd._require_focused_editable(require_search=True))
        for xml in cases:
            with self.subTest(xml=xml), patch.object(pdd, "dump_ui_xml", return_value=xml):
                with self.assertRaises(pdd.AdbError):
                    pdd._require_focused_editable(require_search=True)
        with patch.object(pdd, "dump_ui_xml", return_value=pdd.UiDump(tree(), compressed=True)):
            with self.assertRaises(pdd.AdbError):
                pdd._require_focused_editable(require_search=True)
        with patch.object(pdd, "dump_ui_xml", return_value=str(tree())):
            with self.assertRaisesRegex(pdd.AdbError, "происхождение"):
                pdd._require_focused_editable(require_search=True)

    def test_enter_requires_search_not_chat_or_user_text(self):
        for xml in (tree(rid="chat_input"), tree(rid="chat_input", text="搜索"),
                    tree(rid="chat_input", extra=f'<node package="{pdd.PH["package"]}" text="搜索" bounds="[900,1000][1050,1090]" />')):
            with self.subTest(xml=xml), patch.object(pdd, "dump_ui_xml", return_value=xml):
                with self.assertRaisesRegex(pdd.AdbError, "Контекст поиска"):
                    pdd._require_focused_editable(require_search=True)
        search_button = f'<node package="{pdd.PH["package"]}" text="搜索" bounds="[900,150][1050,240]" />'
        with patch.object(pdd, "dump_ui_xml", return_value=tree(rid="field", extra=search_button)):
            self.assertTrue(pdd._require_focused_editable(require_search=True))

    def test_text_enter_is_rechecked_and_never_sent_after_focus_change(self):
        args = types.SimpleNamespace(file=None, text="camera", clear=False, enter=True)
        for changed in (tree(focused="false"), tree(rid="chat_input"), tree(bounds="[100,150][900,240]")):
            with self.subTest(changed=changed), patch.object(pdd, "dump_ui_xml", side_effect=[tree(), tree(), changed]), \
                 patch.object(pdd, "adbkeyboard_status", return_value={"installed": False, "authenticity": "not_installed"}), patch.object(pdd, "run_adb") as adb:
                with self.assertRaises(pdd.AdbError):
                    pdd.cmd_text(args)
                self.assertEqual([c.args[0][:3] for c in adb.call_args_list], [["shell", "input", "text"]])

    def test_ime_restored_when_recheck_fails(self):
        args = types.SimpleNamespace(file=None, text="hello", clear=False, enter=True)
        def adb(cmd, **kwargs):
            return "com.original/.Keyboard" if cmd[:3] == ["shell", "settings", "get"] else ""
        with patch.object(pdd, "dump_ui_xml", side_effect=[tree(), tree(focused="false")]), \
             patch.object(pdd, "adbkeyboard_status", return_value={"installed": True, "authenticity": "authentic"}), \
             patch.object(pdd, "run_adb", side_effect=adb) as run:
            with self.assertRaises(pdd.AdbError):
                pdd.cmd_text(args)
        self.assertTrue(any(c.args[0] == ["shell", "ime", "set", "com.original/.Keyboard"] for c in run.call_args_list))
        self.assertFalse(any(c.args[0][:3] == ["shell", "input", "keyevent"] for c in run.call_args_list))

    def test_non_search_plain_text_requires_focus_but_not_search(self):
        args = types.SimpleNamespace(file=None, text="hello", clear=False, enter=False)
        with patch.object(pdd, "dump_ui_xml", return_value=tree(rid="chat_input")), \
             patch.object(pdd, "adbkeyboard_status", return_value={"installed": False, "authenticity": "not_installed"}), patch.object(pdd, "run_adb") as adb:
            pdd.cmd_text(args)
            self.assertEqual(adb.call_count, 1)
        with patch.object(pdd, "dump_ui_xml", return_value=tree(focused="false")), \
             patch.object(pdd, "adbkeyboard_status", return_value={"installed": False, "authenticity": "not_installed"}), patch.object(pdd, "run_adb") as adb:
            with self.assertRaises(pdd.AdbError):
                pdd.cmd_text(args)
            adb.assert_not_called()

    def test_output_never_reveals_text_or_its_base64(self):
        secret = "FAKE-SECRET-123"
        args = types.SimpleNamespace(file=None, text=secret, clear=False, enter=False)
        captured = []
        with patch.object(pdd, "dump_ui_xml", return_value=tree()), \
             patch.object(pdd, "adbkeyboard_status", return_value={"installed": True, "authenticity": "authentic"}), \
             patch.object(pdd, "run_adb", return_value="") as adb, \
             patch.object(pdd, "out", side_effect=captured.append):
            pdd.cmd_text(args)
        payload = json.dumps(captured, ensure_ascii=False, default=str)
        b64 = base64.b64encode(secret.encode("utf-8")).decode()
        self.assertNotIn(secret, payload)
        self.assertNotIn(b64, payload)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["chars"], len(secret))
        sent = [call.args[0] for call in adb.call_args_list]
        self.assertTrue(any(b64 in command for command in sent), "base64 должен уходить только на устройство")

    def test_clear_without_adbkeyboard_is_refused_before_input(self):
        args = types.SimpleNamespace(file=None, text="hello", clear=True, enter=False)
        with patch.object(pdd, "dump_ui_xml", return_value=tree()), \
             patch.object(pdd, "adbkeyboard_status", return_value={"installed": False, "authenticity": "not_installed"}), \
             patch.object(pdd, "run_adb") as adb:
            with self.assertRaisesRegex(pdd.AdbError, "clear"):
                pdd.cmd_text(args)
            adb.assert_not_called()

    def test_adbkeyboard_clear_is_verified_before_text(self):
        args = types.SimpleNamespace(file=None, text="hello", clear=True, enter=False)
        with patch.object(pdd, "dump_ui_xml",
                          side_effect=[tree(text="old"), tree(text="old"), tree(text="")]), \
             patch.object(pdd, "adbkeyboard_status", return_value={"installed": True, "authenticity": "authentic"}), \
             patch.object(pdd, "run_adb", return_value="") as adb:
            pdd.cmd_text(args)
        sent = [call.args[0] for call in adb.call_args_list]
        clear = ["shell", "am", "broadcast", "-p", pdd.ADBKEYBOARD_PACKAGE, "-a", "ADB_CLEAR_TEXT"]
        self.assertIn(clear, sent)
        clear_index = sent.index(clear)
        input_index = next(i for i, command in enumerate(sent) if "ADB_INPUT_B64" in command)
        self.assertLess(clear_index, input_index)

    def test_adbkeyboard_clear_failure_blocks_text(self):
        args = types.SimpleNamespace(file=None, text="hello", clear=True, enter=False)
        with patch.object(pdd, "dump_ui_xml",
                          side_effect=[tree(text="old"), tree(text="old"), tree(text="still old")]), \
             patch.object(pdd, "adbkeyboard_status", return_value={"installed": True, "authenticity": "authentic"}), \
             patch.object(pdd, "run_adb", return_value="") as adb:
            with self.assertRaisesRegex(pdd.AdbError, "Очистка"):
                pdd.cmd_text(args)
        sent = [call.args[0] for call in adb.call_args_list]
        self.assertFalse(any("ADB_INPUT_B64" in command for command in sent))

    def test_adbkeyboard_clear_without_text_attribute_is_not_assumed(self):
        args = types.SimpleNamespace(file=None, text="hello", clear=True, enter=False)
        missing_text = pdd.UiDump(str(tree(text="old")).replace(' text="old"', ""))
        with patch.object(pdd, "dump_ui_xml", side_effect=[tree(text="old"), tree(text="old"), missing_text]), \
             patch.object(pdd, "adbkeyboard_status", return_value={"installed": True, "authenticity": "authentic"}), \
             patch.object(pdd, "run_adb", return_value="") as adb:
            with self.assertRaisesRegex(pdd.AdbError, "атрибут text"):
                pdd.cmd_text(args)
        sent = [call.args[0] for call in adb.call_args_list]
        self.assertFalse(any("ADB_INPUT_B64" in command for command in sent))

    def test_unverified_keyboard_warns_but_never_claims_authentic(self):
        args = types.SimpleNamespace(file=None, text="hello", clear=False, enter=False)
        captured = []
        with patch.object(pdd, "dump_ui_xml", return_value=tree()), \
             patch.object(pdd, "adbkeyboard_status", return_value={
                 "installed": True, "authenticity": "unknown", "reason": "нет доступа"}), \
             patch.object(pdd, "run_adb", return_value=""), \
             patch.object(pdd, "out", side_effect=captured.append):
            pdd.cmd_text(args)
        self.assertEqual(captured[0]["adbkeyboard_authenticity"], "unknown")
        self.assertIn("warning", captured[0])

    def test_sensitive_adb_timeout_hides_argv_and_secret(self):
        secret = "FAKE-SECRET-123"
        target = "192.0.2.10:37000"
        with patch.object(pdd.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired(cmd=["adb"], timeout=30)):
            with self.assertRaises(pdd.AdbError) as ctx:
                pdd.run_adb(["pair", target, secret], sensitive=True, redact=(secret,))
        message = str(ctx.exception)
        self.assertNotIn(secret, message)
        self.assertNotIn(target, message)

    def test_pair_output_does_not_reveal_code(self):
        secret = "FAKE-SECRET-123"
        captured = []
        with patch.object(pdd, "run_adb", return_value=f"paired {secret}") as adb, \
             patch.object(pdd, "out", side_effect=captured.append):
            pdd.cmd_pair(types.SimpleNamespace(target="192.0.2.10:37000", code=secret))
        payload = json.dumps(captured, ensure_ascii=False)
        self.assertNotIn(secret, payload)
        self.assertIn("***", captured[0]["result"])
        self.assertTrue(adb.call_args.kwargs.get("sensitive"))

    def test_input_timeout_does_not_reveal_text(self):
        secret = "FAKE-SECRET-123"
        args = types.SimpleNamespace(file=None, text=secret, clear=False, enter=False)

        def adb(args, **kwargs):
            if list(args[:3]) == ["shell", "input", "text"]:
                raise subprocess.TimeoutExpired(cmd=["adb"], timeout=40)
            return ""

        with patch.object(pdd, "dump_ui_xml", return_value=tree()), \
             patch.object(pdd, "adbkeyboard_status", return_value={"installed": False, "authenticity": "not_installed"}), \
             patch.object(pdd, "run_adb", side_effect=adb):
            with self.assertRaises(pdd.AdbError) as ctx:
                pdd.cmd_text(args)
        self.assertNotIn(secret, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
