#!/usr/bin/env python
"""chat.py — фоновая переписка с веб-чатами (Google AI Pro / ChatGPT Business) через ваш Chrome + CDP. Без ИИ.

Запуск браузера (держится как фоновая задача):
  python chat.py browser [--mode headless|offscreen|visible]
Однократный вход в аккаунты (обычное окно Chrome без автоматизации, закройте его после входа):
  python chat.py login
Работа:
  python chat.py status
  python chat.py task "подобрать демисезонную куртку до 200¥"
  python chat.py send --to gemini --text-file msg.txt --image a.jpg --image b.jpg
  python chat.py read --to gemini           # последний ответ
  python chat.py new --to chatgpt           # новый чат (манифест уйдёт автоматически)
  python chat.py set-url --to gemini URL    # продолжить в конкретном чате
  python chat.py primary chatgpt            # сменить основного стратега
  python chat.py inspect --to gemini        # диагностика селекторов
"""
import argparse
import base64
import json
import mimetypes
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from common import ROOT, StateError, load_config, load_state, log_dir, out, save_config, update_state

CFG = load_config()
BR = CFG["browser"]
PORT = int(BR.get("cdp_port", 9333))
PROFILE = ROOT / "profile"
PROVIDERS = ("gemini", "chatgpt")


class ChatError(RuntimeError):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


# ----------------------------------------------------------------- Browser process
def cdp_alive():
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=1):
            return True
    except OSError:
        return False


def profile_in_use():
    lock = PROFILE / "lockfile"
    if not lock.exists():
        return False
    try:
        os.remove(lock)
        return False
    except OSError:
        return True


def chrome_path():
    p = BR.get("chrome_path")
    if p and Path(p).exists():
        return p
    for c in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")):
        if Path(c).exists():
            return c
    raise ChatError("NO_CHROME", "Не найден Google Chrome — укажите browser.chrome_path в config.yaml")


def cmd_login(a):
    if cdp_alive():
        raise ChatError("BROWSER_RUNNING", "Фоновый браузер уже запущен — остановите его перед входом.")
    if profile_in_use():
        raise ChatError("PROFILE_IN_USE", "Окно Chrome с этим профилем уже открыто.")
    PROFILE.mkdir(exist_ok=True)
    urls = [CFG["chats"][p]["url"] for p in PROVIDERS]
    proc = subprocess.Popen([chrome_path(), f"--user-data-dir={PROFILE}", "--no-first-run",
                             "--no-default-browser-check", "--new-window", *urls])
    out("Открыто окно Chrome (отдельный профиль pdd-operator). Войдите в Google AI Pro и ChatGPT Business, "
        "затем просто ЗАКРОЙТЕ окно — сессии сохранятся.")
    proc.wait()
    time.sleep(2)
    out({"ok": True, "note": "Окно входа закрыто, профиль сохранён."})


def cmd_browser(a):
    if cdp_alive():
        out({"ok": True, "note": f"Браузер уже работает на порту {PORT}"})
        return
    if profile_in_use():
        raise ChatError("PROFILE_IN_USE", "Профиль занят открытым окном Chrome (вход?). Закройте его.")
    PROFILE.mkdir(exist_ok=True)
    mode = a.mode or BR.get("mode", "headless")
    flags = [f"--user-data-dir={PROFILE}", f"--remote-debugging-port={PORT}", "--no-first-run",
             "--no-default-browser-check", "--window-size=1366,900",
             "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
             "--disable-backgrounding-occluded-windows"]
    if mode == "headless":
        flags.append("--headless=new")
    elif mode == "offscreen":
        flags.append("--window-position=-3000,-3000")
    proc = subprocess.Popen([chrome_path(), *flags, "about:blank"])
    for _ in range(30):
        if cdp_alive():
            break
        time.sleep(0.5)
    out({"ok": cdp_alive(), "mode": mode, "port": PORT, "pid": proc.pid,
         "note": "Браузер работает. Эта задача должна оставаться запущенной."})
    proc.wait()


# ----------------------------------------------------------------- CDP session
class Session:
    def __enter__(self):
        if not cdp_alive():
            raise ChatError("BROWSER_DOWN", "Фоновый браузер не запущен. Запустите фоновую задачу: python chat.py browser")
        from playwright.sync_api import sync_playwright
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}", timeout=20000)
        self.ctx = self.browser.contexts[0] if self.browser.contexts else self.browser.new_context()
        return self

    def __exit__(self, *exc):
        try:
            self.pw.stop()
        except Exception:
            pass


def provider_cfg(p):
    if p not in PROVIDERS:
        raise ChatError("BAD_PROVIDER", f"Неизвестный чат {p}; доступны {PROVIDERS}")
    return CFG["chats"][p]


def _valid_target(url, host):
    """Accept only the configured provider's HTTPS origin, not substring lookalikes."""
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname == host and parsed.port in (None, 443)
                and not parsed.username and not parsed.password)
    except (TypeError, ValueError):
        return False


def get_page(s: Session, p, url=None):
    c = provider_cfg(p)
    st = load_state()
    marker = st["chats"][p].get("prepared_id") if not url else None
    if marker:
        for candidate in s.ctx.pages:
            try:
                if candidate.evaluate("() => window.name") == marker:
                    return candidate
            except Exception:
                continue
        raise ChatError("PREPARED_TAB_MISSING", "Подготовленная вкладка не найдена; подготовьте и проверьте её заново")
    target = url or st["chats"][p].get("url") or c["url"]
    if not _valid_target(target, c["host"]):
        raise ChatError("BAD_CHAT_URL", "Адрес чата не принадлежит выбранному сервису HTTPS")
    # Select the exact conversation, never an arbitrary tab on the same host.
    page = next((pg for pg in s.ctx.pages if pg.url.rstrip("/") == target.rstrip("/")), None)
    if page is None:
        page = s.ctx.new_page()
        page.goto(target, wait_until="domcontentloaded", timeout=60000)
    return page


def check_ready(page, c, wait_ms=25000):
    try:
        title = page.title()
    except Exception:
        title = ""
    if re.search(r"just a moment|один момент|attention required|verify you are human", title, re.I):
        return "CHALLENGE"
    try:
        page.wait_for_selector(c["editor"], state="visible", timeout=wait_ms)
        return "OK"
    except Exception:
        u = page.url
        if "accounts.google.com" in u or "/auth" in u or "login" in u:
            return "NEED_LOGIN"
        try:
            body = page.inner_text("body", timeout=3000)[:2000]
        except Exception:
            body = ""
        if re.search(r"Log in|Sign in|Войти|Sign up", body) and len(body) < 1500:
            return "NEED_LOGIN"
        if re.search(r"verify you are human|подтвердите, что вы человек|unusual activity", body, re.I):
            return "CHALLENGE"
        return "NO_EDITOR"


def assistant_texts(page, c):
    items = page.locator(c["assistant"])
    n = items.count()
    return n, items


def last_reply_text(page, c):
    n, items = assistant_texts(page, c)
    if n == 0:
        return ""
    last = items.nth(n - 1)
    sub = c.get("assistant_text")
    try:
        if sub and last.locator(sub).count():
            return "\n".join(t.strip() for t in last.locator(sub).all_inner_texts()).strip()
        return last.inner_text(timeout=5000).strip()
    except Exception:
        return ""


def is_generating(page, c):
    try:
        loc = page.locator(c["stop"])
        return any(loc.nth(i).is_visible() for i in range(min(loc.count(), 3)))
    except Exception:
        return False


JS_PASTE = """
async ({selector, files, text}) => {
  const el = document.querySelector(selector);
  if (!el) return "no-editor";
  el.focus();
  const dt = new DataTransfer();
  for (const f of (files || [])) {
    const bin = atob(f.b64); const arr = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
    dt.items.add(new File([arr], f.name, {type: f.mime}));
  }
  if (text) dt.setData("text/plain", text);
  el.dispatchEvent(new ClipboardEvent("paste", {clipboardData: dt, bubbles: true, cancelable: true}));
  return "ok";
}
"""


def _file_payload(paths):
    res = []
    for p in paths:
        p = Path(p)
        res.append({"name": p.name, "mime": mimetypes.guess_type(p.name)[0] or "image/jpeg",
                    "b64": base64.b64encode(p.read_bytes()).decode()})
    return res


def attach(page, c, files):
    if not files:
        return None
    errors = []
    first_sel = c["editor"].split(",")[0].strip()
    for method in c.get("attach_methods", ["input", "paste", "chooser"]):
        try:
            if method == "input":
                inputs = page.locator(c["file_input"])
                cnt = inputs.count()
                for i in range(cnt):
                    acc = inputs.nth(i).get_attribute("accept") or ""
                    if acc and "image" not in acc and "*" not in acc:
                        continue
                    inputs.nth(i).set_input_files([str(f) for f in files])
                    return "input"
                errors.append("input: нет подходящего input[type=file]")
            elif method == "paste":
                sel = next((s.strip() for s in c["editor"].split(",") if page.locator(s.strip()).count()), first_sel)
                r = page.evaluate(JS_PASTE, {"selector": sel, "files": _file_payload(files), "text": None})
                if r == "ok":
                    page.wait_for_timeout(2500)
                    return "paste"
                errors.append(f"paste: {r}")
            elif method == "chooser":
                clicks = c.get("upload_clicks") or []
                if not clicks:
                    continue
                with page.expect_file_chooser(timeout=10000) as fc:
                    for sel in clicks:
                        page.locator(sel).first.click(timeout=5000)
                        page.wait_for_timeout(600)
                fc.value.set_files([str(f) for f in files])
                return "chooser"
        except Exception as e:
            errors.append(f"{method}: {str(e)[:160]}")
    raise ChatError("ATTACH_FAILED", "Не удалось прикрепить изображения: " + " | ".join(errors))


def _editor_value(ed):
    tag = ed.evaluate("el => el.tagName.toLowerCase()")
    return ed.input_value() if tag in ("textarea", "input") else ed.inner_text()


def _empty_editor(page, c):
    sel = next((s.strip() for s in c["editor"].split(",") if page.locator(s.strip()).count()), None)
    if not sel:
        raise ChatError("NO_EDITOR", "Не найдено поле ввода")
    ed = page.locator(sel).first
    if _editor_value(ed).strip():
        raise ChatError("EDITOR_NOT_EMPTY", "Поле чата содержит текст; отправка остановлена, чтобы не затронуть чужой черновик")
    return sel, ed


def put_text(page, c, text):
    sel, ed = _empty_editor(page, c)
    ed.click(timeout=5000)
    page.evaluate(JS_PASTE, {"selector": sel, "files": [], "text": text})
    page.wait_for_timeout(500)
    def clean(value):
        return "\n".join(line.strip() for line in value.replace("\r\n", "\n").replace("\xa0", " ").splitlines() if line.strip())
    got = clean(_editor_value(ed))
    if got != clean(text):
        # The editor was empty before our insertion; replace only our own draft.
        ed.fill(text)
        page.wait_for_timeout(300)
        if clean(_editor_value(ed)) != clean(text):
            raise ChatError("TEXT_MISMATCH", "Текст в поле не совпадает с сообщением; отправка остановлена")
    return sel


def click_send(page, c, timeout=180):
    deadline = time.time() + timeout
    btn = page.locator(c["send"])
    while time.time() < deadline:
        try:
            cnt = btn.count()
            for i in reversed(range(cnt)):
                b = btn.nth(i)
                if b.is_visible() and b.is_enabled() and (b.get_attribute("aria-disabled") or "false") != "true":
                    b.click(timeout=5000)
                    page.wait_for_timeout(1000)
                    return "button"
        except Exception:
            pass
        page.wait_for_timeout(500)
    raise ChatError("SEND_DISABLED", "Кнопка отправки так и не стала активной (загрузка файла зависла?)")


def wait_reply(page, c, before, timeout):
    deadline = time.time() + timeout
    while assistant_texts(page, c)[0] <= before:
        if time.time() > deadline:
            raise ChatError("NO_REPLY", "Ответ не появился")
        if time.time() > deadline - timeout + 180 and not is_generating(page, c) and assistant_texts(page, c)[0] <= before:
            # 180 с без нового ответа и без генерации — вероятно, не отправилось или лимит
            body = page.inner_text("body")[-1500:]
            raise ChatError("NO_REPLY", "Ответ не начался за 90 с. Возможен лимит/ошибка. Хвост страницы: " + body)
        time.sleep(1)
    stable_need = int(BR.get("stable_seconds", 4))
    prev, stable = None, 0
    while time.time() < deadline:
        txt = last_reply_text(page, c)
        if txt and txt == prev and not is_generating(page, c):
            stable += 1
            if stable >= stable_need:
                return txt
        else:
            stable = 0
        prev = txt
        time.sleep(1)
    raise ChatError("TIMEOUT", "Ответ не завершился вовремя. Частичный текст:\n" + (prev or ""))


# ----------------------------------------------------------------- Manifest
def build_manifest(p):
    st = load_state()
    other = "chatgpt" if p == "gemini" else "gemini"
    tpl = (ROOT / "prompts" / "manifest.md").read_text(encoding="utf-8")
    return (tpl.replace("{SELF_CHAT}", CFG["chats"][p]["title"])
               .replace("{OTHER_CHAT}", CFG["chats"][other]["title"])
               .replace("{TASK}", (st.get("task") if st.get("task_context_valid") else None) or "(задача будет сообщена следующим сообщением)")
               .replace("{PROFILE}", "Только данные, явно переданные в сообщении по этой задаче.")
               .replace("{CLOTHING}", ""))


# ----------------------------------------------------------------- Commands
def parse_action(text):
    m = list(re.finditer(r"ACTION:\s*([A-Z_]+)\s*[:\-]?\s*(.*)", text))
    if not m:
        return None
    return {"type": m[-1].group(1), "arg": m[-1].group(2).strip()}


def _same_context(st, task_id, p, conversation_id):
    return st.get("task_id") == task_id and st["chats"][p].get("conversation_id") == conversation_id


def _update_context(p, task_id, conversation_id, changes, require_no_pending=False):
    def mutate(st):
        if not _same_context(st, task_id, p, conversation_id):
            raise ChatError("TASK_CHANGED", "Задача или беседа изменилась; прежний ответ не записан в новую задачу")
        if require_no_pending and st["chats"][p].get("pending_send"):
            raise ChatError("PENDING_SEND", "Состояние отправки изменилось; операция остановлена")
        st["chats"][p].update(changes)
    update_state(mutate)


def _check_no_prior_attachments(page, confirmed, editor_selector):
    # Neither provider exposes a documented exhaustive draft-attachment API.
    # Explicit visual confirmation is required even when no common indicators appear.
    if not confirmed:
        raise ChatError("ATTACHMENTS_UNVERIFIED", "Осмотрите подготовленную вкладку и её редактор. Если старых вложений нет, укажите --confirmed-no-prior-attachments")
    try:
        probe = page.evaluate("""selector => {
      const visible = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
      const editor = document.querySelector(selector);
      if (!editor) return null;
      const composer = editor.closest('form') || editor.closest('rich-textarea') || editor.parentElement;
      if (!composer) return null;
      const inputs = [...document.querySelectorAll('input[type=file]')];
      const files = inputs.some(e => e.files && e.files.length);
      const chips = [...composer.querySelectorAll(`[data-testid*="attachment" i], [data-testid*="file-preview" i], [aria-label*="remove file" i], [aria-label*="remove attachment" i], [aria-label*="удалить файл" i]`)].some(visible);
      return {files, chips};
    }""", editor_selector)
    except Exception as e:
        raise ChatError("ATTACHMENTS_UNVERIFIED", "Не удалось проверить признаки вложений; отправка остановлена") from e
    if not isinstance(probe, dict) or "files" not in probe or "chips" not in probe:
        raise ChatError("ATTACHMENTS_UNVERIFIED", "Не удалось проверить доступные признаки вложений; отправка остановлена")
    if probe["files"] or probe["chips"]:
        raise ChatError("EDITOR_HAS_ATTACHMENTS", "Обнаружены вложения в редакторе; отправка остановлена")


def cmd_send(a):
    p = a.to or CFG["chats"]["primary"]
    c = provider_cfg(p)
    text = Path(a.text_file).read_text(encoding="utf-8") if a.text_file else (a.text or "")
    files = [Path(f) for f in (a.image or [])]
    st = load_state()
    task_id = st.get("task_id")
    conversation_id = st["chats"][p].get("conversation_id")
    if st["chats"][p].get("pending_send"):
        raise ChatError("PENDING_SEND", "Предыдущая отправка могла состояться. Сначала проверьте чат; повторная отправка заблокирована")
    # Old persistent state might contain a previous task's ref. Only a ref
    # explicitly registered by the current version's task command is eligible.
    ref_img = st.get("ref_image") if st.get("task_context_valid") else None
    if ref_img and not st["chats"][p].get("ref_sent"):
        ref_path = Path(ref_img)
        if not ref_path.is_file():
            raise ChatError("NO_FILE", f"Нет файла референса текущей задачи: {ref_path}")
        if all(f.resolve() != ref_path.resolve() for f in files if f.exists()):
            files.insert(0, ref_path)
    for f in files:
        if not f.exists():
            raise ChatError("NO_FILE", f"Нет файла {f}")
    with_manifest = not st["chats"][p].get("manifest_sent") or a.manifest
    if with_manifest:
        text = build_manifest(p) + "\n\n---\n\n" + text
    with Session() as s:
        if not st["chats"][p].get("prepared_id"):
            raise ChatError("PREPARE_REQUIRED", "Сначала подготовьте вкладку командой prepare и осмотрите редактор")
        page = get_page(s, p)
        status = check_ready(page, c)
        if status != "OK":
            raise ChatError(status, f"{c['title']}: {status}. URL: {page.url}")
        if not _valid_target(page.url, c["host"]):
            raise ChatError("BAD_CHAT_URL", "После перехода открыт другой сайт; отправка остановлена")
        inspected_url = st["chats"][p].get("inspected_url")
        if not inspected_url or page.url != inspected_url:
            raise ChatError("CHAT_MISMATCH", "Адрес вкладки изменился после осмотра; повторите inspect")
        expected = st["chats"][p].get("url")
        if expected and not st["chats"][p].get("new_task_pending") and page.url.rstrip("/") != expected.rstrip("/"):
            raise ChatError("CHAT_MISMATCH", "Открыт другой чат; отправка остановлена")
        before = assistant_texts(page, c)[0]
        editor_selector, _ = _empty_editor(page, c)
        _check_no_prior_attachments(page, getattr(a, "confirmed_no_prior_attachments", False), editor_selector)
        method = attach(page, c, files)
        put_text(page, c, text.strip())
        # Persist uncertainty BEFORE the click: a crash/timeout cannot silently retry.
        before_send = {key: st["chats"][p].get(key) for key in ("manifest_sent", "ref_sent", "new_task_pending", "messages")}
        ref_included = bool(ref_img and any(f.resolve() == Path(ref_img).resolve() for f in files))
        _update_context(p, task_id, conversation_id, {"pending_send": True, "pending_previous": before_send, "inspected_url": None,
                        "pending_manifest": with_manifest, "pending_ref": ref_included}, require_no_pending=True)
        try:
            click_send(page, c)
            _update_context(p, task_id, conversation_id, {"pending_send": True, "new_task_pending": False,
                "manifest_sent": bool(with_manifest or st["chats"][p].get("manifest_sent")),
                "ref_sent": bool(st["chats"][p].get("ref_sent") or (ref_img and any(f.resolve() == Path(ref_img).resolve() for f in files)))})
            reply = wait_reply(page, c, before, a.timeout or int(BR.get("reply_timeout_seconds", 600)))
            url = page.url
        except Exception:
            # Keep pending; preserve the selected URL if still on the same task.
            try:
                if _valid_target(page.url, c["host"]):
                    _update_context(p, task_id, conversation_id, {"url": page.url})
            except ChatError:
                pass
            raise
    changes = {"pending_send": False, "pending_previous": None, "pending_manifest": False, "pending_ref": False, "new_task_pending": False,
               "messages": int(st["chats"][p].get("messages", 0)) + 1}
    if _valid_target(url, c["host"]):
        changes["url"] = url
    _update_context(p, task_id, conversation_id, changes)
    n = len(list(log_dir("chats").glob(f"{p}_*.md"))) + 1
    lp = log_dir("chats") / f"{p}_{n:04d}.md"
    # Default journal is metadata only. Persist content only on explicit opt-in.
    if getattr(a, "log_content", False):
        log = f"# SENT ({method}, files={len(files)})\n\n{text}\n\n# REPLY\n\n{reply}\n"
    else:
        log = f"# Chat metadata\nprovider={p}\nattachment_method={method}\nfiles_requested={len(files)}\nattachment_delivery=unverified\ncontent_saved=false\n"
    lp.write_text(log, encoding="utf-8")
    act = parse_action(reply)
    out(f"=== REPLY {p} | manifest={'yes' if with_manifest else 'no'} | attach={method} | files_requested={len(files)} | attachment_delivery=unverified | log={lp} ===\n"
        f"{reply}\n=== PARSED ACTION: {json.dumps(act, ensure_ascii=False)} ===")


def cmd_read(a):
    p = a.to or CFG["chats"]["primary"]
    c = provider_cfg(p)
    with Session() as s:
        page = get_page(s, p)
        txt = last_reply_text(page, c)
        gen = is_generating(page, c)
    out(f"=== LAST REPLY {p} | generating={gen} ===\n{txt}\n=== PARSED ACTION: "
        f"{json.dumps(parse_action(txt), ensure_ascii=False)} ===")


def cmd_new(a):
    p = a.to
    c = provider_cfg(p)
    before = load_state()
    if before["chats"][p].get("pending_send"):
        raise ChatError("PENDING_SEND", "Сначала проверьте предыдущую отправку и разрешите её состояние")
    task_id, conv_id = before.get("task_id"), before["chats"][p].get("conversation_id")
    marker = "pdd-operator-" + uuid.uuid4().hex
    with Session() as s:
        page = s.ctx.new_page()
        page.goto(c["url"], wait_until="domcontentloaded", timeout=60000)
        status = check_ready(page, c)
        if status != "OK" or not _valid_target(page.url, c["host"]):
            raise ChatError(status if status != "OK" else "BAD_CHAT_URL", "Не удалось подготовить вкладку чата")
        page.evaluate("name => { window.name = name; }", marker)
    _update_context(p, task_id, conv_id, {"url": None, "manifest_sent": False, "ref_sent": False, "messages": 0,
                    "new_task_pending": True, "prepared_id": marker, "inspected_url": None,
                    "conversation_id": uuid.uuid4().hex}, require_no_pending=True)
    out({"ok": True, "status": status, "chat": p, "note": "Создана отдельная вкладка; осмотрите её перед отправкой."})


def cmd_prepare(a):
    st = load_state()
    p = a.to
    if st["chats"][p].get("pending_send"):
        raise ChatError("PENDING_SEND", "Сначала проверьте предыдущую отправку и разрешите её состояние")
    if st["chats"][p].get("new_task_pending"):
        cmd_new(a)
        return
    c = provider_cfg(p)
    task_id, conv_id = st.get("task_id"), st["chats"][p].get("conversation_id")
    marker = "pdd-operator-" + uuid.uuid4().hex
    with Session() as s:
        page = get_page(s, p)
        status = check_ready(page, c)
        if status != "OK" or not _valid_target(page.url, c["host"]):
            raise ChatError(status if status != "OK" else "BAD_CHAT_URL", "Не удалось подготовить вкладку чата")
        page.evaluate("name => { window.name = name; }", marker)
    _update_context(p, task_id, conv_id, {"prepared_id": marker, "inspected_url": None}, require_no_pending=True)
    out({"ok": True, "chat": p, "note": "Вкладка подготовлена; осмотрите её перед отправкой."})


def cmd_resolve(a):
    if not (a.reviewed_sent or a.reviewed_not_sent):
        raise ChatError("REVIEW_REQUIRED", "После проверки укажите --reviewed-sent либо --reviewed-not-sent")
    p = a.to
    def mutate(st):
        if not st["chats"][p].get("pending_send"):
            raise ChatError("NO_PENDING_SEND", "Ожидающей отправки нет")
        ch = st["chats"][p]
        previous = ch.get("pending_previous")
        if not isinstance(previous, dict):
            raise ChatError("PENDING_METADATA_MISSING", "Не хватает состояния попытки; начните новую задачу после проверки")
        if a.reviewed_sent:
            ch.update({"manifest_sent": bool(previous.get("manifest_sent") or ch.get("pending_manifest")),
                       "ref_sent": bool(previous.get("ref_sent") or ch.get("pending_ref")),
                       "new_task_pending": False, "messages": int(previous.get("messages") or 0) + 1})
        else:
            ch.update(previous)
        ch.update({"pending_send": False, "pending_previous": None, "pending_manifest": False, "pending_ref": False})
    update_state(mutate)
    out({"ok": True, "chat": p, "note": "Блокировка снята после проверки; автоматический повтор не выполнялся."})


def cmd_set_url(a):
    p = a.to
    c = provider_cfg(p)
    st = load_state()
    if st["chats"][p].get("pending_send"):
        raise ChatError("PENDING_SEND", "Сначала проверьте предыдущую отправку и разрешите её состояние")
    task_id, conv_id = st.get("task_id"), st["chats"][p].get("conversation_id")
    with Session() as s:
        page = get_page(s, p, url=a.url)
        status = check_ready(page, c)
    known = a.url in st["manifest_urls"]
    _update_context(p, task_id, conv_id, {"url": a.url, "manifest_sent": known, "new_task_pending": False,
                    "prepared_id": None, "inspected_url": None, "conversation_id": uuid.uuid4().hex}, require_no_pending=True)
    out({"ok": status == "OK", "status": status, "chat": p, "manifest_already_sent": known})


def cmd_primary(a):
    provider_cfg(a.provider)
    CFG["chats"]["primary"] = a.provider
    save_config(CFG)
    out({"ok": True, "primary": a.provider})


def cmd_task(a):
    task_file = getattr(a, "text_file", None)
    if task_file and a.text:
        raise ChatError("BAD_TASK", "Укажите текст задачи только одним способом: позиционно или --text-file")
    if task_file:
        task_text = Path(task_file).read_text(encoding="utf-8-sig")
    elif a.text:
        task_text = a.text
    else:
        raise ChatError("BAD_TASK", "Укажите текст задачи через --text-file")
    if not task_text.strip():
        raise ChatError("BAD_TASK", "Текст задачи пуст")
    ref = None
    if getattr(a, "ref_image", None):
        rp = Path(a.ref_image).resolve()
        if not rp.exists():
            raise ChatError("NO_FILE", f"Нет файла референса {rp}")
        ref = str(rp)
    def mutate(st):
        st["task"] = task_text.strip()
        st["task_id"] = uuid.uuid4().hex
        st["task_context_valid"] = True
        st["ref_image"] = ref
        for p in PROVIDERS:
            if p in st.get("chats", {}):
                st["chats"][p].update({"url": None, "manifest_sent": False, "ref_sent": False, "messages": 0,
                                       "new_task_pending": True, "prepared_id": None, "inspected_url": None, "pending_send": False,
                                       "conversation_id": uuid.uuid4().hex})
    update_state(mutate)
    out({"ok": True, "task": task_text.strip(), "ref_image": ref, "note": "Следующая отправка начнёт новую беседу и получит контекст текущей задачи."})


def cmd_status(a):
    st = load_state()
    res = {"browser": cdp_alive(), "port": PORT, "primary": CFG["chats"]["primary"], "task": st.get("task"), "chats": {}}
    if res["browser"]:
        with Session() as s:
            for p in PROVIDERS:
                c = CFG["chats"][p]
                try:
                    page = get_page(s, p)
                    res["chats"][p] = {"status": check_ready(page, c, wait_ms=20000), "url": page.url,
                                       "manifest_sent": st["chats"][p].get("manifest_sent")}
                except Exception as e:
                    res["chats"][p] = {"status": "ERROR", "error": str(e)[:300]}
    else:
        res["hint"] = "Запустите фоновую задачу: python chat.py browser"
    out(res)


def cmd_inspect(a):
    p = a.to
    c = provider_cfg(p)
    st = load_state()
    if not st["chats"][p].get("prepared_id"):
        raise ChatError("PREPARE_REQUIRED", "Сначала подготовьте вкладку командой prepare")
    task_id, conv_id = st.get("task_id"), st["chats"][p].get("conversation_id")
    with Session() as s:
        page = get_page(s, p)
        if not _valid_target(page.url, c["host"]):
            raise ChatError("BAD_CHAT_URL", "Подготовленная вкладка перешла на другой сайт")
        page.wait_for_timeout(1500)
        info = page.evaluate("""() => {
          const pick = (el) => ({tag: el.tagName.toLowerCase(), id: el.id || undefined,
             cls: (el.className && el.className.baseVal === undefined ? String(el.className) : '').slice(0, 80) || undefined,
             aria: el.getAttribute('aria-label') || undefined, testid: el.getAttribute('data-testid') || el.getAttribute('data-test-id') || undefined,
             disabled: el.disabled || el.getAttribute('aria-disabled') || undefined, accept: el.getAttribute('accept') || undefined});
          return {
            url: location.href, title: document.title,
            editables: [...document.querySelectorAll('[contenteditable="true"], textarea')].map(el => ({...pick(el),
              hasText: !!(el.tagName.toLowerCase() === 'textarea' ? el.value : el.innerText).trim()})),
            files: [...document.querySelectorAll('input[type=file]')].map(el => ({...pick(el), hasFiles: !!(el.files && el.files.length)})),
            buttons: [...document.querySelectorAll('button')].filter(b => b.offsetParent !== null).slice(-60).map(pick),
          };
        }""")
        info["configured"] = {k: page.locator(c[k]).count() for k in ("editor", "send", "stop", "assistant")}
        if getattr(a, "screenshot", None):
            target = Path(a.screenshot).resolve()
            if target.exists():
                raise ChatError("FILE_EXISTS", "Файл снимка уже существует; укажите новый путь")
            if target.suffix.lower() != ".png" or not target.parent.is_dir():
                raise ChatError("BAD_SCREENSHOT_PATH", "Укажите новый PNG-файл в существующей папке")
            page.screenshot(path=str(target), full_page=False)
            info["screenshot"] = str(target)
            info["screenshot_note"] = "Снимок может содержать личные данные и черновики; храните его защищённо и не пересылайте автоматически."
        inspected_url = page.url
        if not _valid_target(inspected_url, c["host"]):
            raise ChatError("BAD_CHAT_URL", "Вкладка изменила сайт во время осмотра")
        _update_context(p, task_id, conv_id, {"inspected_url": inspected_url})
    out(info)


def main():
    ap = argparse.ArgumentParser(description="Веб-чаты Стратегов для Оператора Pinduoduo")
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("browser"); p.add_argument("--mode", choices=["headless", "offscreen", "visible"])
    sp.add_parser("login")
    sp.add_parser("status")
    p = sp.add_parser("task"); p.add_argument("text", nargs="?"); p.add_argument("--text-file", help="UTF-8 файл с текстом задачи"); p.add_argument("--ref-image", help="путь к фото-референсу текущей задачи (автоматически прикрепится при первом сообщении)")
    p = sp.add_parser("send"); p.add_argument("--to", choices=PROVIDERS); p.add_argument("--text-file")
    p.add_argument("--text"); p.add_argument("--image", action="append"); p.add_argument("--timeout", type=int)
    p.add_argument("--manifest", action="store_true", help="принудительно приложить манифест")
    p.add_argument("--log-content", action="store_true", help="явно сохранить полный текст сообщения и ответа в локальный журнал")
    p.add_argument("--confirmed-no-prior-attachments", action="store_true", help="только после осмотра текущей вкладки: в редакторе нет чужих вложений")
    p = sp.add_parser("read"); p.add_argument("--to", choices=PROVIDERS)
    p = sp.add_parser("new"); p.add_argument("--to", choices=PROVIDERS, required=True)
    p = sp.add_parser("prepare"); p.add_argument("--to", choices=PROVIDERS, required=True)
    p = sp.add_parser("resolve"); p.add_argument("--to", choices=PROVIDERS, required=True)
    g = p.add_mutually_exclusive_group(required=True); g.add_argument("--reviewed-sent", action="store_true"); g.add_argument("--reviewed-not-sent", action="store_true")
    p = sp.add_parser("set-url"); p.add_argument("--to", choices=PROVIDERS, required=True); p.add_argument("url")
    p = sp.add_parser("primary"); p.add_argument("provider", choices=PROVIDERS)
    p = sp.add_parser("inspect"); p.add_argument("--to", choices=PROVIDERS, required=True); p.add_argument("--screenshot", help="необязательный новый PNG-файл локального снимка выбранной вкладки (может содержать личные данные)")
    a = ap.parse_args()
    fn = {"browser": cmd_browser, "login": cmd_login, "status": cmd_status, "task": cmd_task, "send": cmd_send,
          "read": cmd_read, "new": cmd_new, "prepare": cmd_prepare, "resolve": cmd_resolve, "set-url": cmd_set_url, "primary": cmd_primary,
          "inspect": cmd_inspect}[a.cmd]
    try:
        fn(a)
    except ChatError as e:
        out({"ok": False, "code": e.code, "error": str(e)})
        sys.exit(2)
    except StateError as e:
        out({"ok": False, "code": "STATE_ERROR", "error": str(e)})
        sys.exit(2)


if __name__ == "__main__":
    main()
