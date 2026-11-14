"""Проверка CDP без реального браузера: только заглушки HTTP/TCP.

Категория (б): логика ветвей проверена синтетическими ответами. Реальный
Chrome, профиль и сетевой стек здесь не запускаются.
"""
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("pdd_chat_cdp_test", ROOT / "chat.py")
chat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(chat)


def version_payload(browser="Chrome/131.0.0.0", endpoint=None):
    return {
        "Browser": browser,
        "Protocol-Version": "1.3",
        "webSocketDebuggerUrl": endpoint or f"ws://127.0.0.1:{chat.PORT}/devtools/browser/synthetic-id",
    }


class CdpProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fake-profile-")
        self.addCleanup(self.temp.cleanup)
        self.profile = Path(self.temp.name)
        patcher = patch.object(chat, "PROFILE", self.profile)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_marker(self, port=None, path="/devtools/browser/synthetic-id"):
        (self.profile / "DevToolsActivePort").write_text(f"{port or chat.PORT}\n{path}\n", encoding="utf-8")

    def probe_with(self, version, targets=None, marker=True):
        def fake_http(path, timeout=1.5):
            if path == "/json/version":
                if isinstance(version, Exception):
                    raise version
                return version
            if path == "/json/list":
                if isinstance(targets, Exception):
                    raise targets
                return targets if targets is not None else [{"id": "1", "type": "page", "url": "about:blank"}]
            raise AssertionError(path)
        if marker:
            self.write_marker()
        with patch.object(chat, "_tcp_reachable", return_value=True), \
                patch.object(chat, "_http_json", side_effect=fake_http):
            return chat.cdp_probe()

    def test_http_json_rejects_unparsable_response(self):
        class Response:
            status = 200
            def read(self, size):
                return b"this is not json"
        class Connection:
            def __init__(self, *args, **kwargs): pass
            def request(self, *args, **kwargs): pass
            def getresponse(self): return Response()
            def close(self): pass
        with patch.object(chat.http.client, "HTTPConnection", Connection):
            with self.assertRaises(ValueError):
                chat._http_json("/json/version")

    def test_closed_port_is_not_verified(self):
        with patch.object(chat, "_tcp_reachable", return_value=False), \
                patch.object(chat, "_http_json") as http:
            result = chat.cdp_probe()
        self.assertFalse(result["reachable"])
        self.assertFalse(result["verified"])
        self.assertEqual(result["reason"], "port_closed")
        http.assert_not_called()
        self.assertFalse(chat.cdp_alive())

    def test_foreign_process_on_port_is_rejected(self):
        result = self.probe_with(ValueError("not JSON"))
        self.assertTrue(result["reachable"])
        self.assertFalse(result["verified"])
        self.assertEqual(result["reason"], "not_cdp")

    def test_non_chrome_browser_string_is_rejected(self):
        result = self.probe_with(version_payload(browser="nginx/1.24"))
        self.assertFalse(result["verified"])
        self.assertEqual(result["reason"], "unexpected_browser")

    def test_ws_endpoint_for_another_port_is_rejected(self):
        result = self.probe_with(version_payload(endpoint="ws://127.0.0.1:1/devtools/browser/x"))
        self.assertFalse(result["verified"])
        self.assertEqual(result["reason"], "endpoint_mismatch")

    def test_unparsable_target_list_is_rejected(self):
        result = self.probe_with(version_payload(), targets=ValueError("broken JSON"))
        self.assertFalse(result["verified"])
        self.assertEqual(result["reason"], "bad_targets")

    def test_missing_profile_marker_is_a_safe_refusal(self):
        result = self.probe_with(version_payload(), marker=False)
        self.assertFalse(result["verified"])
        self.assertFalse(result["profile_verified"])
        self.assertEqual(result["reason"], "profile_marker_missing")

    def test_profile_marker_for_another_instance_is_rejected(self):
        self.write_marker(path="/devtools/browser/other-instance")
        def fake_http(path, timeout=1.5):
            if path == "/json/version":
                return version_payload()
            return [{"id": "1", "type": "page", "url": "about:blank"}]
        with patch.object(chat, "_tcp_reachable", return_value=True), \
                patch.object(chat, "_http_json", side_effect=fake_http):
            result = chat.cdp_probe()
        self.assertFalse(result["verified"])
        self.assertEqual(result["reason"], "profile_marker_mismatch")

    def test_matching_chrome_and_profile_marker_is_verified(self):
        result = self.probe_with(version_payload())
        self.assertTrue(result["verified"])
        self.assertTrue(result["profile_verified"])
        self.assertEqual(result["reason"], "ok")
        self.assertEqual(result["browser"], "Chrome/131.0.0.0")
        self.write_marker()
        def fake_http(path, timeout=1.5):
            if path == "/json/version":
                return version_payload()
            return [{"id": "1", "type": "page", "url": "about:blank"}]
        with patch.object(chat, "_tcp_reachable", return_value=True), \
                patch.object(chat, "_http_json", side_effect=fake_http):
            self.assertTrue(chat.cdp_alive())

    def test_session_refuses_unverified_or_down_browser(self):
        with patch.object(chat, "cdp_probe", return_value={"verified": False, "reachable": True,
                                                             "reason": "not_cdp"}):
            with self.assertRaises(chat.ChatError) as unverified:
                chat.Session().__enter__()
        self.assertEqual(unverified.exception.code, "BROWSER_UNVERIFIED")
        with patch.object(chat, "cdp_probe", return_value={"verified": False, "reachable": False,
                                                             "reason": "port_closed"}):
            with self.assertRaises(chat.ChatError) as down:
                chat.Session().__enter__()
        self.assertEqual(down.exception.code, "BROWSER_DOWN")

    def test_browser_command_refuses_foreign_port_without_spawning(self):
        with patch.object(chat, "cdp_probe", return_value={"verified": False, "reachable": True,
                                                             "reason": "not_cdp"}), \
                patch.object(chat, "subprocess") as subprocess:
            with self.assertRaises(chat.ChatError) as err:
                chat.cmd_browser(types.SimpleNamespace(mode=None))
        self.assertEqual(err.exception.code, "CDP_UNVERIFIED")
        subprocess.Popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()