"""Offline regression checks: these tests never connect to Chrome or a phone."""
import copy
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("pdd_chat_safety_test", ROOT / "chat.py")
chat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(chat)


def state():
    return {"task": "old private task", "ref_image": "old-private.jpg", "manifest_urls": [],
            "chats": {p: {"url": f"https://{chat.CFG['chats'][p]['host']}/old", "manifest_sent": True,
                          "ref_sent": False, "messages": 3} for p in chat.PROVIDERS}}


class Page:
    def __init__(self, url="about:blank"):
        self.url = url
        self.visited = []
        self.name = ""

    def goto(self, url, **kwargs):
        self.visited.append(url)
        self.url = url

    def evaluate(self, expression, arg=None):
        if "window.name =" in expression:
            self.name = arg
            return None
        if "window.name" in expression:
            return self.name
        return {"files": False, "chips": False}


class Context:
    def __init__(self, pages=()):
        self.pages = list(pages)

    def new_page(self):
        page = Page()
        self.pages.append(page)
        return page


class SafetyTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(chat, "update_state", side_effect=AssertionError("test must mock state mutation"))
        guard.start()
        self.addCleanup(guard.stop)

    def test_task_from_utf8_file_resets_old_context_and_starts_new_chats(self):
        st = state()
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "task.txt"
            f.write_text("Найти 商品 для кожи", encoding="utf-8-sig")
            with patch.object(chat, "load_state", return_value=st), patch.object(chat, "update_state", side_effect=lambda fn: fn(st)) as save, patch.object(chat, "out"):
                chat.cmd_task(types.SimpleNamespace(text=None, text_file=str(f), ref_image=None))
            self.assertEqual(st["task"], "Найти 商品 для кожи")
            self.assertIsNone(st["ref_image"])
            for p in chat.PROVIDERS:
                self.assertIsNone(st["chats"][p]["url"])
                self.assertFalse(st["chats"][p]["manifest_sent"])
                self.assertTrue(st["chats"][p]["new_task_pending"])
            save.assert_called_once()

    def test_task_rejects_two_sources_without_modifying_state(self):
        st = state()
        before = copy.deepcopy(st)
        with patch.object(chat, "load_state", return_value=st), patch.object(chat, "update_state", side_effect=lambda fn: fn(st)) as save:
            with self.assertRaises(chat.ChatError):
                chat.cmd_task(types.SimpleNamespace(text="one", text_file="other.txt", ref_image=None))
        self.assertEqual(before, st)
        save.assert_not_called()

    def test_manifest_excludes_stale_task_profile_and_clothes(self):
        st = state()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "prompts").mkdir()
            (root / "prompts" / "manifest.md").write_text("{TASK}|{PROFILE}|{CLOTHING}", encoding="utf-8")
            with patch.object(chat, "ROOT", root), patch.object(chat, "load_state", return_value=st):
                result = chat.build_manifest("gemini")
        self.assertNotIn("old private task", result)
        self.assertNotIn("возраст", result)
        self.assertNotIn("old-private.jpg", result)

    def test_target_selection_matches_exact_chat_not_first_host_tab(self):
        st = state()
        other = Page("https://gemini.google.com/app/unrelated")
        target = Page(st["chats"]["gemini"]["url"])
        ctx = Context([other, target])
        with patch.object(chat, "load_state", return_value=st):
            found = chat.get_page(types.SimpleNamespace(ctx=ctx), "gemini")
        self.assertIs(found, target)
        self.assertEqual(other.visited, [])

    def test_untrusted_url_is_rejected_before_opening_page(self):
        st = state()
        st["chats"]["gemini"]["url"] = "https://gemini.google.com.attacker.test/app"
        ctx = Context()
        with patch.object(chat, "load_state", return_value=st), self.assertRaises(chat.ChatError):
            chat.get_page(types.SimpleNamespace(ctx=ctx), "gemini")
        self.assertEqual(ctx.pages, [])

    def test_corrupt_state_is_reported_as_a_structured_cli_error(self):
        with patch.object(sys, "argv", ["chat.py", "status"]), \
                patch.object(chat, "cmd_status", side_effect=chat.StateError("state is damaged")), patch.object(chat, "out") as out:
            with self.assertRaises(SystemExit) as err:
                chat.main()
        self.assertEqual(err.exception.code, 2)
        self.assertEqual(out.call_args.args[0]["code"], "STATE_ERROR")

    def test_old_reference_is_not_automatically_included_and_log_omits_text(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            old_ref = d / "old.jpg"
            old_ref.write_bytes(b"old")
            st = state()
            st["ref_image"] = str(old_ref)
            st["chats"]["gemini"]["manifest_sent"] = True
            input_text = d / "message.txt"
            input_text.write_text("private incoming message", encoding="utf-8")
            page = Page("https://gemini.google.com/app/old")
            page.name = "prepared"
            st["chats"]["gemini"].update(url=page.url, prepared_id="prepared", inspected_url=page.url)
            class FakeSession:
                def __enter__(self): return types.SimpleNamespace(ctx=Context([page]))
                def __exit__(self, *args): pass
            args = types.SimpleNamespace(to="gemini", text_file=str(input_text), text=None, image=[],
                                         timeout=1, manifest=False, log_content=False)
            with patch.object(chat, "load_state", return_value=st), patch.object(chat, "update_state", side_effect=lambda fn: fn(st)), \
                    patch.object(chat, "Session", FakeSession), patch.object(chat, "check_ready", return_value="OK"), \
                    patch.object(chat, "assistant_texts", return_value=(0, None)), patch.object(chat, "_empty_editor", return_value=("#editor", None)), patch.object(chat, "_check_no_prior_attachments"), \
                    patch.object(chat, "attach", return_value=None) as attach, patch.object(chat, "put_text"), \
                    patch.object(chat, "click_send"), patch.object(chat, "wait_reply", return_value="private response"), \
                    patch.object(chat, "log_dir", return_value=d), patch.object(chat, "out") as out:
                chat.cmd_send(args)
            self.assertEqual(attach.call_args.args[2], [])
            log = next(d.glob("gemini_*.md")).read_text(encoding="utf-8")
            self.assertNotIn("private incoming message", log)
            self.assertNotIn("private response", log)
            self.assertIn("attachment_delivery=unverified", log)
            self.assertIn("attachment_delivery=unverified", out.call_args.args[0])

    def test_nonempty_editor_rejected(self):
        editor = types.SimpleNamespace(evaluate=lambda _: "div", inner_text=lambda: "unsent user draft")
        locator = types.SimpleNamespace(count=lambda: 1, first=editor)
        page = types.SimpleNamespace(locator=lambda _: locator)
        with self.assertRaises(chat.ChatError) as e:
            chat._empty_editor(page, {"editor": "#editor"})
        self.assertEqual(e.exception.code, "EDITOR_NOT_EMPTY")

    def test_new_task_uses_fresh_page_and_only_explicit_ref(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            ref = d / "current-ref.jpg"
            ref.write_bytes(b"reference")
            message = d / "message.txt"
            message.write_text("some message", encoding="utf-8")
            st = state()
            st.update(task="current", task_context_valid=True, ref_image=str(ref))
            st["chats"]["gemini"].update(url=None, manifest_sent=False, ref_sent=False, new_task_pending=True)
            unrelated = Page("https://gemini.google.com/app/unrelated")
            ctx = Context([unrelated])
            class FakeSession:
                def __enter__(self): return types.SimpleNamespace(ctx=ctx)
                def __exit__(self, *args): pass
            args = types.SimpleNamespace(to="gemini", text_file=str(message), text=None, image=[],
                                         timeout=1, manifest=False, log_content=False)
            with patch.object(chat, "load_state", return_value=st), patch.object(chat, "update_state", side_effect=lambda fn: fn(st)), \
                    patch.object(chat, "build_manifest", return_value="current task"), patch.object(chat, "Session", FakeSession), \
                    patch.object(chat, "check_ready", return_value="OK"), patch.object(chat, "assistant_texts", return_value=(0, None)), \
                    patch.object(chat, "_empty_editor", return_value=("#editor", None)), patch.object(chat, "_check_no_prior_attachments"), patch.object(chat, "attach", return_value="paste") as attach, \
                    patch.object(chat, "put_text"), patch.object(chat, "click_send"), patch.object(chat, "wait_reply", return_value="reply"), \
                    patch.object(chat, "log_dir", return_value=d), patch.object(chat, "out"):
                chat.cmd_prepare(types.SimpleNamespace(to="gemini"))
                st["chats"]["gemini"]["inspected_url"] = ctx.pages[-1].url
                chat.cmd_send(args)
            self.assertEqual(len(ctx.pages), 2)
            self.assertEqual(unrelated.visited, [])
            self.assertEqual(attach.call_args.args[2], [ref])
            self.assertFalse(st["chats"]["gemini"]["new_task_pending"])
            self.assertTrue(st["chats"]["gemini"]["ref_sent"])

    def test_explicit_task_ref_missing_does_not_replace_previous_context(self):
        st = state()
        before = copy.deepcopy(st)
        with patch.object(chat, "load_state", return_value=st), patch.object(chat, "update_state", side_effect=lambda fn: fn(st)) as save:
            with self.assertRaises(chat.ChatError):
                chat.cmd_task(types.SimpleNamespace(text="new", text_file=None, ref_image="missing-file-481.png"))
        self.assertEqual(st, before)
        save.assert_not_called()

    def test_textarea_draft_uses_value_not_inner_text(self):
        editor = types.SimpleNamespace(evaluate=lambda _: "textarea", inner_text=lambda: "", input_value=lambda: "private draft")
        page = types.SimpleNamespace(locator=lambda _: types.SimpleNamespace(count=lambda: 1, first=editor))
        with self.assertRaises(chat.ChatError) as err:
            chat._empty_editor(page, {"editor": "#editor"})
        self.assertEqual(err.exception.code, "EDITOR_NOT_EMPTY")

    def test_unconfirmed_or_detected_attachment_fails_closed(self):
        page = types.SimpleNamespace(evaluate=lambda *args: {"files": True, "chips": False})
        with self.assertRaises(chat.ChatError) as unverified:
            chat._check_no_prior_attachments(page, False, "#editor")
        self.assertEqual(unverified.exception.code, "ATTACHMENTS_UNVERIFIED")
        with self.assertRaises(chat.ChatError) as detected:
            chat._check_no_prior_attachments(page, True, "#editor")
        self.assertEqual(detected.exception.code, "EDITOR_HAS_ATTACHMENTS")

    def test_missing_prepared_tab_never_falls_back_to_another(self):
        st = state()
        st["chats"]["gemini"]["prepared_id"] = "missing-marker"
        ctx = Context([Page("https://gemini.google.com/app/old")])
        with patch.object(chat, "load_state", return_value=st), self.assertRaises(chat.ChatError) as err:
            chat.get_page(types.SimpleNamespace(ctx=ctx), "gemini")
        self.assertEqual(err.exception.code, "PREPARED_TAB_MISSING")
        self.assertEqual(len(ctx.pages), 1)

    def test_navigation_after_inspection_prevents_send(self):
        st = state()
        st["chats"]["gemini"].update(prepared_id="prepared", inspected_url="https://gemini.google.com/app/original", new_task_pending=True)
        page = Page("https://gemini.google.com/app/another")
        page.name = "prepared"
        class FakeSession:
            def __enter__(self): return types.SimpleNamespace(ctx=Context([page]))
            def __exit__(self, *args): pass
        args = types.SimpleNamespace(to="gemini", text_file=None, text="test", image=[], manifest=False,
                                     confirmed_no_prior_attachments=True)
        with patch.object(chat, "load_state", return_value=st), patch.object(chat, "Session", FakeSession), \
                patch.object(chat, "check_ready", return_value="OK"), patch.object(chat, "attach") as attach:
            with self.assertRaises(chat.ChatError) as err:
                chat.cmd_send(args)
        self.assertEqual(err.exception.code, "CHAT_MISMATCH")
        attach.assert_not_called()

    def test_inspect_screenshot_only_on_request_and_no_overwrite(self):
        st = state()
        st["chats"]["gemini"]["prepared_id"] = "prepared"
        page = Page("https://gemini.google.com/app")
        page.name = "prepared"
        page.wait_for_timeout = lambda _: None
        page.locator = lambda _: types.SimpleNamespace(count=lambda: 0)
        calls = []
        page.screenshot = lambda **kwargs: calls.append(kwargs)
        ctx = Context([page])
        class FakeSession:
            def __enter__(self): return types.SimpleNamespace(ctx=ctx)
            def __exit__(self, *args): pass
        with tempfile.TemporaryDirectory() as d, patch.object(chat, "load_state", return_value=st), \
                patch.object(chat, "Session", FakeSession), patch.object(chat, "out"), patch.object(chat, "update_state", side_effect=lambda fn: fn(st)):
            chat.cmd_inspect(types.SimpleNamespace(to="gemini", screenshot=None))
            self.assertEqual(calls, [])
            target = Path(d) / "inspection.png"
            chat.cmd_inspect(types.SimpleNamespace(to="gemini", screenshot=str(target)))
            self.assertEqual(calls[0]["path"], str(target))
            target.write_bytes(b"existing")
            with self.assertRaises(chat.ChatError) as err:
                chat.cmd_inspect(types.SimpleNamespace(to="gemini", screenshot=str(target)))
            self.assertEqual(err.exception.code, "FILE_EXISTS")
            self.assertEqual(len(calls), 1)

    def test_new_creates_separate_tab_does_not_navigate_existing_draft(self):
        st = state()
        existing = Page("https://gemini.google.com/app/old")
        ctx = Context([existing])
        class FakeSession:
            def __enter__(self): return types.SimpleNamespace(ctx=ctx)
            def __exit__(self, *args): pass
        with patch.object(chat, "load_state", return_value=st), patch.object(chat, "update_state", side_effect=lambda fn: fn(st)), \
                patch.object(chat, "Session", FakeSession), patch.object(chat, "check_ready", return_value="OK"), patch.object(chat, "out"):
            chat.cmd_new(types.SimpleNamespace(to="gemini"))
        self.assertEqual(existing.visited, [])
        self.assertEqual(len(ctx.pages), 2)
        self.assertEqual(ctx.pages[1].name, st["chats"]["gemini"]["prepared_id"])

    def test_missing_current_ref_stops_before_browser(self):
        st = state()
        st.update(task_context_valid=True, ref_image="missing-current-ref-209.jpg")
        args = types.SimpleNamespace(to="gemini", text_file=None, text="test", image=[], manifest=False)
        with patch.object(chat, "load_state", return_value=st), patch.object(chat, "Session") as session:
            with self.assertRaises(chat.ChatError) as err:
                chat.cmd_send(args)
        self.assertEqual(err.exception.code, "NO_FILE")
        session.assert_not_called()

    def test_timeout_blocks_repeated_send_and_preserves_attempt(self):
        st = state()
        st.update(task_id="task1", task_context_valid=True, ref_image=None)
        page = Page(st["chats"]["gemini"]["url"])
        page.name = "prepared"
        st["chats"]["gemini"].update(prepared_id="prepared", inspected_url=page.url)
        ctx = Context([page])
        class FakeSession:
            def __enter__(self): return types.SimpleNamespace(ctx=ctx)
            def __exit__(self, *args): pass
        args = types.SimpleNamespace(to="gemini", text_file=None, text="test", image=[], timeout=1,
                                     manifest=False, log_content=False, confirmed_no_prior_attachments=True)
        with patch.object(chat, "load_state", return_value=st), patch.object(chat, "update_state", side_effect=lambda fn: fn(st)), \
                patch.object(chat, "Session", FakeSession), patch.object(chat, "check_ready", return_value="OK"), \
                patch.object(chat, "assistant_texts", return_value=(0, None)), patch.object(chat, "_empty_editor", return_value=("#editor", None)), \
                patch.object(chat, "_check_no_prior_attachments"), patch.object(chat, "attach", return_value=None), \
                patch.object(chat, "put_text"), patch.object(chat, "click_send") as click, \
                patch.object(chat, "wait_reply", side_effect=chat.ChatError("TIMEOUT", "late")):
            with self.assertRaises(chat.ChatError) as first:
                chat.cmd_send(args)
            self.assertEqual(first.exception.code, "TIMEOUT")
            self.assertTrue(st["chats"]["gemini"]["pending_send"])
            with self.assertRaises(chat.ChatError) as second:
                chat.cmd_send(args)
            self.assertEqual(second.exception.code, "PENDING_SEND")
            self.assertEqual(click.call_count, 1)

    def test_pending_cannot_be_resolved_without_explicit_review(self):
        st = state()
        st["chats"]["gemini"].update(pending_send=True, pending_previous={"manifest_sent": False, "ref_sent": False, "messages": 0}, pending_manifest=True, pending_ref=True)
        with patch.object(chat, "load_state", return_value=st), patch.object(chat, "update_state", side_effect=lambda fn: fn(st)) as save, patch.object(chat, "out"):
            with self.assertRaises(chat.ChatError) as err:
                chat.cmd_resolve(types.SimpleNamespace(to="gemini", reviewed_sent=False, reviewed_not_sent=False))
            self.assertEqual(err.exception.code, "REVIEW_REQUIRED")
            self.assertTrue(st["chats"]["gemini"]["pending_send"])
            save.assert_not_called()
            chat.cmd_resolve(types.SimpleNamespace(to="gemini", reviewed_sent=False, reviewed_not_sent=True))
            self.assertFalse(st["chats"]["gemini"]["pending_send"])
            self.assertFalse(st["chats"]["gemini"]["manifest_sent"])
            self.assertFalse(st["chats"]["gemini"]["ref_sent"])
            save.assert_called_once()

    def test_resolve_reviewed_sent_commits_only_attempted_flags(self):
        st = state()
        st["chats"]["gemini"].update(pending_send=True, pending_previous={"manifest_sent": False, "ref_sent": False, "messages": 2}, pending_manifest=True, pending_ref=False)
        with patch.object(chat, "update_state", side_effect=lambda fn: fn(st)), patch.object(chat, "out"):
            chat.cmd_resolve(types.SimpleNamespace(to="gemini", reviewed_sent=True, reviewed_not_sent=False))
        self.assertTrue(st["chats"]["gemini"]["manifest_sent"])
        self.assertFalse(st["chats"]["gemini"]["ref_sent"])
        self.assertEqual(st["chats"]["gemini"]["messages"], 3)

    def test_task_change_during_wait_cannot_write_old_reply_into_new_task(self):
        st = state()
        st.update(task_id="task1", task_context_valid=True, ref_image=None)
        page = Page(st["chats"]["gemini"]["url"])
        page.name = "prepared"
        st["chats"]["gemini"].update(prepared_id="prepared", inspected_url=page.url)
        ctx = Context([page])
        class FakeSession:
            def __enter__(self): return types.SimpleNamespace(ctx=ctx)
            def __exit__(self, *args): pass
        def change_task(*args):
            st["task_id"] = "task2"
            st["chats"]["gemini"] = {"url": None, "manifest_sent": False, "messages": 0,
                                       "pending_send": False, "conversation_id": "new"}
            return "old response"
        args = types.SimpleNamespace(to="gemini", text_file=None, text="test", image=[], timeout=1,
                                     manifest=False, log_content=False, confirmed_no_prior_attachments=True)
        with patch.object(chat, "load_state", return_value=st), patch.object(chat, "update_state", side_effect=lambda fn: fn(st)), \
                patch.object(chat, "Session", FakeSession), patch.object(chat, "check_ready", return_value="OK"), \
                patch.object(chat, "assistant_texts", return_value=(0, None)), patch.object(chat, "_empty_editor", return_value=("#editor", None)), \
                patch.object(chat, "_check_no_prior_attachments"), patch.object(chat, "attach", return_value=None), \
                patch.object(chat, "put_text"), patch.object(chat, "click_send"), patch.object(chat, "wait_reply", side_effect=change_task):
            with self.assertRaises(chat.ChatError) as err:
                chat.cmd_send(args)
        self.assertEqual(err.exception.code, "TASK_CHANGED")
        self.assertEqual(st["chats"]["gemini"]["messages"], 0)
        self.assertIsNone(st["chats"]["gemini"]["url"])


if __name__ == "__main__":
    unittest.main()
