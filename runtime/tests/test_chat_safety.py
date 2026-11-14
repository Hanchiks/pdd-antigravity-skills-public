"""Offline regression checks: these tests never connect to Chrome or a phone."""
import copy
import importlib.util
import multiprocessing
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import common

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
        work = (Path(__file__).resolve().parent / "state-test-work").resolve()
        work.mkdir(exist_ok=True)
        self.lock_temp = tempfile.TemporaryDirectory(prefix="chat-locks-", dir=work)
        self.addCleanup(self.lock_temp.cleanup)
        lock_patch = patch.object(common, "STATE_PATH", Path(self.lock_temp.name) / "state.json")
        lock_patch.start()
        self.addCleanup(lock_patch.stop)

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

    def test_new_task_does_not_erase_unresolved_pending_send(self):
        st = state()
        st.update(task_id="task-before-crash", task_context_valid=True)
        st["chats"]["gemini"].update(
            pending_send=True,
            pending_previous={"manifest_sent": True, "ref_sent": False, "messages": 1},
            pending_manifest=True,
            pending_ref=False,
            pending_context={"chat": "gemini", "at": "2026-11-14T00:00:00Z",
                             "stage": "sent_unknown", "conversation_id": "conv-before-crash",
                             "files": ["synthetic.jpg"], "delivery": "unknown"})
        before = copy.deepcopy(st)
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "task.txt"
            f.write_text("new synthetic task", encoding="utf-8")
            with patch.object(chat, "load_state", return_value=st), \
                    patch.object(chat, "update_state", side_effect=lambda fn: fn(st)) as save, \
                    patch.object(chat, "out"):
                with self.assertRaises(chat.ChatError) as err:
                    chat.cmd_task(types.SimpleNamespace(text=None, text_file=str(f), ref_image=None))
        self.assertEqual(err.exception.code, "PENDING_SEND")
        self.assertEqual(st, before)
        self.assertTrue(st["chats"]["gemini"]["pending_send"])
        self.assertEqual(st["chats"]["gemini"]["pending_context"]["conversation_id"], "conv-before-crash")
        self.assertNotEqual(st["task"], "new synthetic task")

    def test_resolve_clears_pending_context_only_after_explicit_review(self):
        st = state()
        st["chats"]["gemini"].update(
            pending_send=True,
            pending_previous={"manifest_sent": False, "ref_sent": False, "messages": 0},
            pending_manifest=True,
            pending_ref=False,
            pending_context={"chat": "gemini", "stage": "sent_unknown",
                             "stages": ["requested", "attachments_added", "sent_unknown"],
                             "conversation_id": "conv-1", "delivery": "unknown"})
        with patch.object(chat, "update_state", side_effect=lambda fn: fn(st)), patch.object(chat, "out"):
            with self.assertRaises(chat.ChatError) as err:
                chat.cmd_resolve(types.SimpleNamespace(to="gemini", reviewed_sent=False, reviewed_not_sent=False))
            self.assertEqual(err.exception.code, "REVIEW_REQUIRED")
            self.assertTrue(st["chats"]["gemini"]["pending_send"])
            self.assertIsNotNone(st["chats"]["gemini"]["pending_context"])
            chat.cmd_resolve(types.SimpleNamespace(to="gemini", reviewed_sent=False, reviewed_not_sent=True))
        chat_state = st["chats"]["gemini"]
        self.assertFalse(chat_state["pending_send"])
        self.assertIsNone(chat_state["pending_context"])
        self.assertEqual(chat_state["last_attachment"]["stage"], "operator_reviewed_not_sent")
        self.assertEqual(chat_state["last_attachment"]["delivery"], "not_sent")

    def test_repeated_send_is_blocked_before_browser_while_pending(self):
        st = state()
        st["chats"]["gemini"].update(pending_send=True, pending_previous={"messages": 0},
                                     pending_context={"stage": "sent_unknown"})
        args = types.SimpleNamespace(to="gemini", text_file=None, text="same draft", image=[],
                                     manifest=False, confirmed_no_prior_attachments=True)
        with patch.object(chat, "load_state", return_value=st), patch.object(chat, "Session") as session:
            with self.assertRaises(chat.ChatError) as err:
                chat.cmd_send(args)
        self.assertEqual(err.exception.code, "PENDING_SEND")
        session.assert_not_called()

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

    def test_attachment_stages_and_unverified_delivery_are_persisted(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            first, second = d / "synthetic-a.jpg", d / "synthetic-b.jpg"
            first.write_bytes(b"a")
            second.write_bytes(b"b")
            message = d / "message.txt"
            message.write_text("synthetic message", encoding="utf-8")
            st = state()
            st.update(task_id="task-attachments", task_context_valid=True, ref_image=None)
            page = Page(st["chats"]["gemini"]["url"])
            page.name = "prepared"
            st["chats"]["gemini"].update(prepared_id="prepared", inspected_url=page.url)
            ctx = Context([page])
            class FakeSession:
                def __enter__(self): return types.SimpleNamespace(ctx=ctx)
                def __exit__(self, *args): pass
            args = types.SimpleNamespace(to="gemini", text_file=str(message), text=None,
                                         image=[str(first), str(second)], timeout=1,
                                         manifest=False, log_content=False,
                                         confirmed_no_prior_attachments=True)
            with patch.object(chat, "load_state", return_value=st), \
                    patch.object(chat, "update_state", side_effect=lambda fn: fn(st)), \
                    patch.object(chat, "Session", FakeSession), patch.object(chat, "check_ready", return_value="OK"), \
                    patch.object(chat, "assistant_texts", return_value=(0, None)), \
                    patch.object(chat, "_empty_editor", return_value=("#editor", None)), \
                    patch.object(chat, "_check_no_prior_attachments"), \
                    patch.object(chat, "attach", return_value="input"), patch.object(chat, "put_text"), \
                    patch.object(chat, "click_send"), patch.object(chat, "wait_reply", return_value="reply"), \
                    patch.object(chat, "log_dir", return_value=d), patch.object(chat, "out") as out:
                chat.cmd_send(args)
            chat_state = st["chats"]["gemini"]
            record = chat_state["last_attachment"]
            self.assertEqual(record["files_requested"], 2)
            self.assertEqual(record["files_added"], 2)
            self.assertEqual(record["method"], "input")
            self.assertEqual(record["stage"], "reply_received")
            self.assertEqual(record["delivery"], "unverified")
            self.assertFalse(chat_state["pending_send"])
            self.assertIsNone(chat_state["pending_context"])
            for stage in ("requested", "attachments_added", "text_ready", "send_started",
                          "sent_unknown", "reply_received"):
                self.assertIn(stage, record["stages"])
            log = next(d.glob("gemini_*.md")).read_text(encoding="utf-8")
            self.assertIn("attachment_delivery=unverified", log)
            self.assertIn("attachment_delivery=unverified", out.call_args.args[0])

    def test_partial_attachment_failure_is_explicit_and_not_marked_delivered(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            first, second = d / "synthetic-a.jpg", d / "synthetic-b.jpg"
            first.write_bytes(b"a")
            second.write_bytes(b"b")
            st = state()
            st.update(task_id="task-attachments", task_context_valid=True, ref_image=None)
            page = Page(st["chats"]["gemini"]["url"])
            page.name = "prepared"
            st["chats"]["gemini"].update(prepared_id="prepared", inspected_url=page.url)
            ctx = Context([page])
            class FakeSession:
                def __enter__(self): return types.SimpleNamespace(ctx=ctx)
                def __exit__(self, *args): pass
            args = types.SimpleNamespace(to="gemini", text_file=None, text="synthetic message",
                                         image=[str(first), str(second)], timeout=1,
                                         manifest=False, log_content=False,
                                         confirmed_no_prior_attachments=True)
            with patch.object(chat, "load_state", return_value=st), \
                    patch.object(chat, "update_state", side_effect=lambda fn: fn(st)), \
                    patch.object(chat, "Session", FakeSession), patch.object(chat, "check_ready", return_value="OK"), \
                    patch.object(chat, "assistant_texts", return_value=(0, None)), \
                    patch.object(chat, "_empty_editor", return_value=("#editor", None)), \
                    patch.object(chat, "_check_no_prior_attachments"), \
                    patch.object(chat, "attach", side_effect=chat.ChatError("ATTACH_FAILED", "один файл не добавлен")), \
                    patch.object(chat, "put_text") as put_text, patch.object(chat, "click_send") as click:
                with self.assertRaises(chat.ChatError) as err:
                    chat.cmd_send(args)
            self.assertEqual(err.exception.code, "ATTACH_FAILED")
            put_text.assert_not_called()
            click.assert_not_called()
            chat_state = st["chats"]["gemini"]
            self.assertFalse(chat_state.get("pending_send"))
            record = chat_state["last_attachment"]
            self.assertEqual(record["stage"], "add_failed")
            self.assertEqual(record["delivery"], "not_sent")
            self.assertEqual(record["files_requested"], 2)
            self.assertEqual(record["files_added"], 0)
            self.assertNotIn("reply_received", record["stages"])

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
                patch.object(chat, "_check_no_prior_attachments"), patch.object(chat, "attach", return_value=None) as attach, \
                patch.object(chat, "put_text"), patch.object(chat, "click_send") as click, \
                patch.object(chat, "wait_reply", side_effect=chat.ChatError("TIMEOUT", "late")):
            with self.assertRaises(chat.ChatError) as first:
                chat.cmd_send(args)
            self.assertEqual(first.exception.code, "TIMEOUT")
            self.assertTrue(st["chats"]["gemini"]["pending_send"])
            pending = st["chats"]["gemini"]["pending_context"]
            self.assertEqual(pending["stage"], "sent_unknown")
            self.assertEqual(pending["delivery"], "unknown")
            self.assertEqual(pending["generation"], chat.GENERATION_UNKNOWN)
            self.assertIn("send_started", pending["stages"])
            with self.assertRaises(chat.ChatError) as second:
                chat.cmd_send(args)
            self.assertEqual(second.exception.code, "PENDING_SEND")
            self.assertEqual(click.call_count, 1)
            self.assertEqual(attach.call_count, 1)

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


class _StopLocator:
    def __init__(self, count=0, visible=False, error=None):
        self._count = count
        self._visible = visible
        self._error = error

    def count(self):
        if self._error:
            raise self._error
        return self._count

    def nth(self, _index):
        return self

    def is_visible(self):
        if self._error:
            raise self._error
        return self._visible


class _LocatorPage:
    def __init__(self, locator):
        self._locator = locator

    def locator(self, _selector):
        return self._locator


class _Clock:
    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class GenerationStateTests(unittest.TestCase):
    def test_missing_generation_element_is_unknown_not_finished(self):
        page = _LocatorPage(_StopLocator(count=0))
        self.assertEqual(chat.generation_state(page, {"stop": "#stop"}), chat.GENERATION_UNKNOWN)
        self.assertFalse(chat.is_generating(page, {"stop": "#stop"}))

    def test_locator_error_is_unknown_not_finished(self):
        page = _LocatorPage(_StopLocator(error=RuntimeError("selector changed")))
        self.assertEqual(chat.generation_state(page, {"stop": "#stop"}), chat.GENERATION_UNKNOWN)

    def test_visible_stop_element_means_generating(self):
        page = _LocatorPage(_StopLocator(count=1, visible=True))
        self.assertEqual(chat.generation_state(page, {"stop": "#stop"}), chat.GENERATION_GENERATING)

    def test_present_but_hidden_stop_element_means_finished(self):
        page = _LocatorPage(_StopLocator(count=1, visible=False))
        self.assertEqual(chat.generation_state(page, {"stop": "#stop"}), chat.GENERATION_FINISHED)

    def test_wait_reply_timeout_with_unknown_state_is_not_reported_as_finished(self):
        clock = _Clock()
        page = _LocatorPage(_StopLocator(count=1, visible=False))
        with patch.object(chat.time, "time", clock.time), patch.object(chat.time, "sleep", clock.sleep), \
                patch.object(chat, "assistant_texts", return_value=(1, None)), \
                patch.object(chat, "last_reply_text", return_value="partial text"), \
                patch.object(chat, "generation_state", return_value=chat.GENERATION_UNKNOWN):
            with self.assertRaises(chat.ChatError) as err:
                chat.wait_reply(page, {"stop": "#stop"}, before=0, timeout=5)
        self.assertEqual(err.exception.code, "TIMEOUT")
        self.assertIn("неизвестно", str(err.exception))
        self.assertNotIn("partial text", str(err.exception))

    def test_temporarily_stable_text_is_not_treated_as_finished(self):
        clock = _Clock()
        page = _LocatorPage(_StopLocator(count=1, visible=False))
        texts = iter(["part", "part", "full", "full", "full", "full"])
        with patch.object(chat.time, "time", clock.time), patch.object(chat.time, "sleep", clock.sleep), \
                patch.dict(chat.BR, {"stable_seconds": 4}), \
                patch.object(chat, "assistant_texts", return_value=(1, None)), \
                patch.object(chat, "last_reply_text", side_effect=lambda *_: next(texts, "full")), \
                patch.object(chat, "generation_state", return_value=chat.GENERATION_FINISHED):
            result = chat.wait_reply(page, {"stop": "#stop"}, before=0, timeout=100)
        self.assertEqual(result, "full")
        self.assertEqual(result.generation, chat.GENERATION_FINISHED)

    def test_normal_completion_returns_finished_result(self):
        clock = _Clock()
        page = _LocatorPage(_StopLocator(count=1, visible=False))
        with patch.object(chat.time, "time", clock.time), patch.object(chat.time, "sleep", clock.sleep), \
                patch.dict(chat.BR, {"stable_seconds": 2}), \
                patch.object(chat, "assistant_texts", return_value=(1, None)), \
                patch.object(chat, "last_reply_text", return_value="final answer"), \
                patch.object(chat, "generation_state", return_value=chat.GENERATION_FINISHED):
            result = chat.wait_reply(page, {"stop": "#stop"}, before=0, timeout=100)
        self.assertEqual(result, "final answer")
        self.assertEqual(result.generation, chat.GENERATION_FINISHED)


class _FakeSession:
    def __init__(self, ctx, *args, **kwargs):
        self.ctx = ctx

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class ChatContentOutputTests(unittest.TestCase):
    def _send(self, task_secret, reply_secret, log_content=False, show_content=False):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            message = d / "message.txt"
            message.write_text(task_secret, encoding="utf-8")
            st = state()
            st.update(task_id="task-output", task_context_valid=True, ref_image=None)
            page = Page(st["chats"]["gemini"]["url"])
            page.name = "prepared"
            st["chats"]["gemini"].update(prepared_id="prepared", inspected_url=page.url)
            ctx = Context([page])
            args = types.SimpleNamespace(to="gemini", text_file=str(message), text=None, image=[],
                                         timeout=1, manifest=False, log_content=log_content,
                                         show_content=show_content,
                                         confirmed_no_prior_attachments=True)
            with patch.object(chat, "load_state", return_value=st), \
                    patch.object(chat, "update_state", side_effect=lambda fn: fn(st)), \
                    patch.object(chat, "Session", lambda *a, **k: _FakeSession(ctx)), \
                    patch.object(chat, "check_ready", return_value="OK"), \
                    patch.object(chat, "assistant_texts", return_value=(0, None)), \
                    patch.object(chat, "_empty_editor", return_value=("#editor", None)), \
                    patch.object(chat, "_check_no_prior_attachments"), \
                    patch.object(chat, "attach", return_value=None), patch.object(chat, "put_text"), \
                    patch.object(chat, "click_send"), \
                    patch.object(chat, "wait_reply", return_value=chat.ReplyResult(reply_secret, chat.GENERATION_FINISHED)), \
                    patch.object(chat, "log_dir", return_value=d), patch.object(chat, "out") as output:
                chat.cmd_send(args)
            log = next(d.glob("gemini_*.md")).read_text(encoding="utf-8")
        return output.call_args.args[0], log

    def test_send_hides_task_and_reply_by_default(self):
        out, log = self._send("SECRET-TASK-7781", "SECRET-REPLY-9912")
        self.assertNotIn("SECRET-TASK-7781", out)
        self.assertNotIn("SECRET-REPLY-9912", out)
        self.assertNotIn("SECRET-TASK-7781", log)
        self.assertNotIn("SECRET-REPLY-9912", log)
        self.assertIn("content_saved=false", log)
        self.assertIn("generation=finished", log)
        self.assertIn("generation=finished", out)

    def test_log_content_opt_in_documents_what_is_saved(self):
        out, log = self._send("SECRET-TASK-7781", "SECRET-REPLY-9912", log_content=True)
        self.assertIn("SECRET-TASK-7781", log)
        self.assertIn("SECRET-REPLY-9912", log)
        self.assertNotIn("SECRET-REPLY-9912", out)
        self.assertIn("REPLY (finished)", log)

    def test_show_content_opt_in_only_for_reply(self):
        out, log = self._send("SECRET-TASK-7781", "SECRET-REPLY-9912", show_content=True)
        self.assertIn("SECRET-REPLY-9912", out)
        self.assertNotIn("SECRET-TASK-7781", out)
        self.assertNotIn("SECRET-TASK-7781", log)

    def test_read_hides_reply_without_opt_in_and_reports_unknown_state(self):
        page = Page("https://gemini.google.com/app/old")
        ctx = Context([page])
        with patch.object(chat, "Session", lambda *a, **k: _FakeSession(ctx)), \
                patch.object(chat, "get_page", return_value=page), \
                patch.object(chat, "last_reply_text", return_value="SECRET-REPLY-4455"), \
                patch.object(chat, "generation_state", return_value=chat.GENERATION_UNKNOWN), \
                patch.object(chat, "out") as output:
            chat.cmd_read(types.SimpleNamespace(to="gemini", show_content=False))
        text = output.call_args.args[0]
        self.assertNotIn("SECRET-REPLY-4455", text)
        self.assertIn("generation=unknown", text)
        self.assertIn("может быть неполным", text)
        with patch.object(chat, "Session", lambda *a, **k: _FakeSession(ctx)), \
                patch.object(chat, "get_page", return_value=page), \
                patch.object(chat, "last_reply_text", return_value="SECRET-REPLY-4455"), \
                patch.object(chat, "generation_state", return_value=chat.GENERATION_FINISHED), \
                patch.object(chat, "out") as output:
            chat.cmd_read(types.SimpleNamespace(to="gemini", show_content=True))
        self.assertIn("SECRET-REPLY-4455", output.call_args.args[0])

    def test_task_and_status_do_not_echo_task_text(self):
        secret = "SECRET-TASK-2233"
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "task.txt"
            f.write_text(secret, encoding="utf-8")
            st = state()
            with patch.object(chat, "load_state", return_value=st), \
                    patch.object(chat, "update_state", side_effect=lambda fn: fn(st)), \
                    patch.object(chat, "out") as output:
                chat.cmd_task(types.SimpleNamespace(text=None, text_file=str(f), ref_image=None))
            text = repr(output.call_args.args[0])
            self.assertNotIn(secret, text)
            self.assertIn("task_sha256", text)
            st["task"] = secret
            with patch.object(chat, "load_state", return_value=st), \
                    patch.object(chat, "cdp_probe", return_value={"verified": False, "reachable": False,
                                                                  "reason": "port_closed"}), \
                    patch.object(chat, "out") as output:
                chat.cmd_status(types.SimpleNamespace())
            self.assertNotIn(secret, repr(output.call_args.args[0]))


class StatusPassivityTests(unittest.TestCase):
    def _state(self, url):
        st = state()
        st["chats"]["gemini"]["url"] = url
        return st

    def test_status_does_not_create_context_or_tab_when_page_is_missing(self):
        st = self._state("https://gemini.google.com/app/wanted")
        ctx = Context([])
        with patch.object(chat, "load_state", return_value=st), \
                patch.object(chat, "cdp_probe", return_value={"verified": True, "reachable": True,
                                                              "reason": "ok"}), \
                patch.object(chat, "Session", lambda *a, **k: _FakeSession(ctx)), \
                patch.object(chat, "out") as output:
            chat.cmd_status(types.SimpleNamespace())
        result = output.call_args.args[0]
        self.assertEqual(ctx.pages, [])
        self.assertEqual(result["chats"]["gemini"]["status"], "NOT_OPEN")
        self.assertIsNone(result["chats"]["gemini"]["url"])

    def test_status_reads_existing_page_without_navigation(self):
        url = "https://gemini.google.com/app/open"
        st = self._state(url)
        page = Page(url)
        page.name = ""
        ctx = Context([page])
        with patch.object(chat, "load_state", return_value=st), \
                patch.object(chat, "cdp_probe", return_value={"verified": True, "reachable": True,
                                                              "reason": "ok"}), \
                patch.object(chat, "Session", lambda *a, **k: _FakeSession(ctx)), \
                patch.object(chat, "check_ready", return_value="OK"), \
                patch.object(chat, "out") as output:
            chat.cmd_status(types.SimpleNamespace())
        result = output.call_args.args[0]
        self.assertEqual(result["chats"]["gemini"]["status"], "OK")
        self.assertEqual(result["chats"]["gemini"]["url"], url)
        self.assertEqual(page.visited, [])
        self.assertEqual(len(ctx.pages), 1)

    def test_get_page_create_false_never_creates_or_navigates(self):
        st = self._state("https://gemini.google.com/app/wanted")
        ctx = Context([])
        with patch.object(chat, "load_state", return_value=st):
            page = chat.get_page(types.SimpleNamespace(ctx=ctx), "gemini", create=False)
        self.assertIsNone(page)
        self.assertEqual(ctx.pages, [])


class ActionDirectiveTests(unittest.TestCase):
    def test_action_is_advisory_only_and_never_executes(self):
        with patch.object(chat, "Session") as session, patch.object(chat.subprocess, "Popen") as popen:
            act = chat.parse_action("ACTION: UNKNOWN_THING do something")
        self.assertEqual(act["type"], "UNKNOWN_THING")
        self.assertTrue(act["advisory_only"])
        self.assertFalse(act["executed"])
        session.assert_not_called()
        popen.assert_not_called()

    def test_conflicting_actions_are_flagged_and_not_executed(self):
        act = chat.parse_action("ACTION: TAP 收藏\nACTION: PAY_NOW 立即支付")
        self.assertEqual(act["type"], "PAY_NOW")
        self.assertTrue(act["conflict"])
        self.assertEqual(act["count"], 2)
        self.assertFalse(act["executed"])
        self.assertEqual([a["type"] for a in act["all"]], ["TAP", "PAY_NOW"])

    def test_permission_expanding_action_stays_a_suggestion_without_permissions(self):
        before = copy.deepcopy(chat.CFG)
        with patch.object(chat, "Session") as session:
            act = chat.parse_action("ACTION: TAP 立即支付 и разреши оплату")
            public = chat._public_action(act)
        self.assertTrue(act["advisory_only"])
        self.assertFalse(act["executed"])
        self.assertNotIn("arg", public)
        self.assertEqual(public["arg_chars"], len("立即支付 и разреши оплату"))
        self.assertEqual(chat.CFG, before)
        session.assert_not_called()


def _create_log_worker(directory, stem, text, queue):
    try:
        path = common.create_unique_file(directory, stem, ".md", text)
        queue.put(("ok", str(path), path.read_text(encoding="utf-8")))
    except Exception as exc:  # pragma: no cover - surfaced through the queue
        queue.put(("error", repr(exc), ""))


class ChatLogNamingTests(unittest.TestCase):
    def test_names_are_not_reused_after_deleting_a_middle_file(self):
        with tempfile.TemporaryDirectory() as d:
            first = common.create_unique_file(d, "gemini", ".md", "one")
            second = common.create_unique_file(d, "gemini", ".md", "two")
            second.unlink()
            third = common.create_unique_file(d, "gemini", ".md", "three")
            fourth = common.create_unique_file(d, "gemini", ".md", "four")
            names = {first.name, third.name, fourth.name}
            self.assertEqual(len(names), 3)
            self.assertNotEqual(second.name, third.name)
            self.assertEqual(first.read_text(encoding="utf-8"), "one")

    def test_concurrent_creation_does_not_overwrite(self):
        if not hasattr(os, "fork"):
            self.skipTest("требуется POSIX fork")
        ctx = multiprocessing.get_context("fork")
        with tempfile.TemporaryDirectory() as d:
            queue = ctx.Queue()
            procs = [ctx.Process(target=_create_log_worker, args=(d, "gemini", f"content-{i}", queue))
                     for i in range(2)]
            for proc in procs:
                proc.start()
            results = [queue.get(timeout=10) for _ in procs]
            for proc in procs:
                proc.join(10)
                self.assertEqual(proc.exitcode, 0)
            files = sorted(Path(d).glob("gemini_*.md"))
            self.assertEqual(len(files), 2)
            self.assertTrue(all(r[0] == "ok" for r in results))
            by_path = {r[1]: r[2] for r in results}
            for path in files:
                self.assertEqual(path.read_text(encoding="utf-8"), by_path[str(path)])
            self.assertEqual({p.read_text(encoding="utf-8") for p in files}, {"content-0", "content-1"})


if __name__ == "__main__":
    unittest.main()
