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
import hashlib
import http.client
import json
import mimetypes
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlsplit

from common import (ROOT, StateError, create_unique_file, draft_lock, load_config, load_state,
                    log_dir, out, update_config, update_state)

CFG = load_config()
BR = CFG["browser"]
PORT = int(BR.get("cdp_port", 9333))
PROFILE = ROOT / "profile"
PROVIDERS = ("gemini", "chatgpt")
CDP_MAX_BYTES = 1024 * 1024


class ChatError(RuntimeError):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


# ----------------------------------------------------------------- Browser process
def _tcp_reachable(timeout=1.0):
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=timeout):
            return True
    except OSError:
        return False


def _http_json(path, timeout=1.5):
    """Один HTTP GET к локальному CDP-эндпоинту; без прокси и без внешней сети."""
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=timeout)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        if resp.status != 200:
            raise ValueError(f"CDP HTTP status {resp.status}")
        raw = resp.read(CDP_MAX_BYTES + 1)
        if len(raw) > CDP_MAX_BYTES:
            raise ValueError("CDP response too large")
        return json.loads(raw.decode("utf-8"))
    finally:
        conn.close()


def _read_devtools_active_port():
    """Порт и browser-путь из файла профиля, созданного самим Chrome."""
    try:
        lines = (PROFILE / "DevToolsActivePort").read_text(encoding="utf-8").splitlines()
        if len(lines) < 2:
            return None
        return int(lines[0].strip()), lines[1].strip()
    except (OSError, UnicodeError, ValueError):
        return None


def _valid_cdp_endpoint(value):
    try:
        parsed = urlsplit(value)
        return (parsed.scheme in ("ws", "wss")
                and parsed.hostname in ("127.0.0.1", "localhost", "::1")
                and parsed.port == PORT
                and not parsed.username and not parsed.password
                and parsed.path.startswith("/devtools/browser/"))
    except (TypeError, ValueError):
        return False


def cdp_probe():
    """Проверить, что порт обслуживает именно наш Chrome с нашим профилем.

    Открытый TCP-порт сам по себе ничего не доказывает. Проверяются
    HTTP-эндпоинты CDP ``/json/version`` и ``/json/list``, согласованность
    WebSocket-адреса с портом, а привязка к профилю — через файл
    ``DevToolsActivePort``, который Chrome пишет в ``--user-data-dir``.
    CDP не аутентифицирован, поэтому это проверка согласованности, а не
    криптографическое доказательство: локальный процесс теоретически может
    подделать и HTTP-ответ, и файл профиля. При любом сомнении — отказ.
    """
    result = {"port": PORT, "reachable": False, "verified": False,
              "profile_verified": False, "reason": "port_closed"}
    if not _tcp_reachable():
        return result
    result["reachable"] = True
    try:
        version = _http_json("/json/version")
    except Exception:
        result["reason"] = "not_cdp"
        return result
    if not isinstance(version, dict):
        result["reason"] = "bad_version"
        return result
    browser = version.get("Browser")
    if not isinstance(browser, str) or not re.match(r"^(Chrome|Chromium|HeadlessChrome)/\d", browser):
        result["reason"] = "unexpected_browser"
        return result
    endpoint = version.get("webSocketDebuggerUrl")
    if not _valid_cdp_endpoint(endpoint):
        result["reason"] = "endpoint_mismatch"
        return result
    try:
        targets = _http_json("/json/list")
    except Exception:
        result["reason"] = "bad_targets"
        return result
    if not isinstance(targets, list) or any(
            not isinstance(t, dict) or not isinstance(t.get("id"), str) or not isinstance(t.get("type"), str)
            for t in targets):
        result["reason"] = "bad_targets"
        return result
    marker = _read_devtools_active_port()
    if marker is None:
        result["reason"] = "profile_marker_missing"
        return result
    marker_port, marker_path = marker
    parsed_endpoint = urlsplit(endpoint)
    if marker_port != PORT or marker_path != parsed_endpoint.path:
        result["reason"] = "profile_marker_mismatch"
        return result
    result.update({"verified": True, "profile_verified": True, "reason": "ok",
                   "browser": browser, "protocol": version.get("Protocol-Version"),
                   "targets": len(targets)})
    return result


def cdp_alive():
    """True только для подтверждённого нашего Chrome, а не любого занятия порта."""
    return bool(cdp_probe()["verified"])


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
    probe = cdp_probe()
    if probe["verified"]:
        raise ChatError("BROWSER_RUNNING", "Фоновый браузер уже запущен — остановите его перед входом.")
    if probe["reachable"]:
        raise ChatError("CDP_UNVERIFIED", "Порт CDP занят процессом, который не подтверждён как наш Chrome; "
                                          "вход не начат. Освободите порт вручную.")
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
    probe = cdp_probe()
    if probe["verified"]:
        out({"ok": True, "note": f"Браузер уже работает на порту {PORT}", "cdp": probe})
        return
    if probe["reachable"]:
        raise ChatError("CDP_UNVERIFIED", f"Порт {PORT} занят процессом, который не подтверждён как наш Chrome "
                                          f"({probe['reason']}); новый браузер не запускается.")
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
    probe = cdp_probe()
    out({"ok": probe["verified"], "mode": mode, "port": PORT, "pid": proc.pid, "cdp": probe,
         "note": "Браузер работает. Эта задача должна оставаться запущенной." if probe["verified"]
                 else "Браузер запущен, но CDP не подтвердил наш экземпляр и профиль; не используйте его для отправки."})
    proc.wait()


# ----------------------------------------------------------------- CDP session
class Session:
    def __init__(self, create_context=True):
        self.create_context = create_context

    def __enter__(self):
        probe = cdp_probe()
        if not probe["verified"]:
            if probe["reachable"]:
                raise ChatError("BROWSER_UNVERIFIED", "Порт CDP занят, но это не подтверждённый Chrome с нашим "
                                                     f"профилем ({probe['reason']}); работа остановлена.")
            raise ChatError("BROWSER_DOWN", "Фоновый браузер не запущен. Запустите фоновую задачу: python chat.py browser")
        from playwright.sync_api import sync_playwright
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}", timeout=20000)
        if self.browser.contexts:
            self.ctx = self.browser.contexts[0]
        elif self.create_context:
            self.ctx = self.browser.new_context()
        else:
            self.ctx = None
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


def _find_existing_page(s: Session, p, url=None):
    """Найти подходящую уже открытую вкладку, ничего не создавая и не меняя."""
    c = provider_cfg(p)
    st = load_state()
    marker = st["chats"][p].get("prepared_id") if not url else None
    ctx = getattr(s, "ctx", None)
    pages = getattr(ctx, "pages", []) if ctx is not None else []
    if marker:
        for candidate in pages:
            try:
                if candidate.evaluate("() => window.name") == marker:
                    return candidate
            except Exception:
                continue
        return None
    target = url or st["chats"][p].get("url") or c["url"]
    if not _valid_target(target, c["host"]):
        raise ChatError("BAD_CHAT_URL", "Адрес чата не принадлежит выбранному сервису HTTPS")
    # Select the exact conversation, never an arbitrary tab on the same host.
    return next((pg for pg in pages if pg.url.rstrip("/") == target.rstrip("/")), None)


def get_page(s: Session, p, url=None, create=True):
    c = provider_cfg(p)
    st = load_state()
    marker = st["chats"][p].get("prepared_id") if not url else None
    page = _find_existing_page(s, p, url=url)
    if page is not None:
        return page
    if not create:
        return None
    if marker:
        raise ChatError("PREPARED_TAB_MISSING", "Подготовленная вкладка не найдена; подготовьте и проверьте её заново")
    target = url or st["chats"][p].get("url") or c["url"]
    if not _valid_target(target, c["host"]):
        raise ChatError("BAD_CHAT_URL", "Адрес чата не принадлежит выбранному сервису HTTPS")
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


GENERATION_GENERATING = "generating"
GENERATION_FINISHED = "finished"
GENERATION_UNKNOWN = "unknown"


class ReplyResult(str):
    """Текст ответа вместе с явным состоянием генерации.

    Остаётся строкой для совместимости: старые вызывающие и тестовые заглушки
    могут возвращать обычный ``str``. Состояние доступно как ``.generation``.
    """

    def __new__(cls, text, generation):
        obj = super().__new__(cls, text)
        obj.generation = generation
        return obj


def generation_state(page, c):
    """generating / finished / unknown для текущего ответа.

    Если элементы индикатора генерации не найдены (селектор устарел, страница
    ещё не загрузилась) или проверка недоступна, состояние — ``unknown``, а не
    ``finished``. Неизвестное состояние запрещает заявлять полноту ответа.
    """
    try:
        loc = page.locator(c["stop"])
        count = loc.count()
    except Exception:
        return GENERATION_UNKNOWN
    if count <= 0:
        return GENERATION_UNKNOWN
    try:
        visible = any(loc.nth(i).is_visible() for i in range(min(count, 3)))
    except Exception:
        return GENERATION_UNKNOWN
    return GENERATION_GENERATING if visible else GENERATION_FINISHED


def is_generating(page, c):
    """Совместимость: True только при подтверждённой генерации."""
    return generation_state(page, c) == GENERATION_GENERATING


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
        state = generation_state(page, c)
        if (time.time() > deadline - timeout + 180 and state == GENERATION_FINISHED
                and assistant_texts(page, c)[0] <= before):
            # 180 с без нового ответа и с подтверждённым завершением генерации —
            # вероятно, не отправилось или лимит. Содержимое страницы в ошибку
            # не попадает, чтобы не раскрывать задачу/ответ/секреты.
            raise ChatError("NO_REPLY", "Ответ не начался за 90 с. Возможен лимит/ошибка; "
                                        "содержимое страницы не выводится.")
        time.sleep(1)
    # Минимум два подтверждающих замера после первого: два одинаковых
    # измерения подряд сами по себе не доказывают завершение.
    stable_need = max(2, int(BR.get("stable_seconds", 4)))
    prev, stable = None, 0
    while time.time() < deadline:
        txt = last_reply_text(page, c)
        state = generation_state(page, c)
        if txt and txt == prev and state == GENERATION_FINISHED:
            stable += 1
            if stable >= stable_need:
                return ReplyResult(txt, GENERATION_FINISHED)
        else:
            stable = 0
        prev = txt
        time.sleep(1)
    # Текст мог появиться, но завершение не подтверждено. Не выдаём частичный
    # ответ за полный: состояние остаётся unknown.
    raise ChatError("TIMEOUT", "Ответ не подтверждён как завершённый; состояние неизвестно. "
                               "Частичный текст в ошибку не выводится.")


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
def parse_actions(text):
    """Все директивы ACTION из ответа модели как недоверенные предложения."""
    return [{"type": m.group(1), "arg": m.group(2).strip()}
            for m in re.finditer(r"ACTION:\s*([A-Z_]+)\s*[:\-]?\s*(.*)", text)]


def parse_action(text):
    """Последний ACTION как предложение.

    Ответ веб-модели — недоверенные данные. Разбор ничего не выполняет, не
    выдаёт разрешений и не связан с устройством/браузером. Поля
    ``advisory_only``/``executed`` фиксируют это явно; конфликтующие директивы
    помечаются ``conflict`` и требуют отдельного решения оператора.
    """
    actions = parse_actions(text)
    if not actions:
        return None
    result = dict(actions[-1])
    types = {a["type"] for a in actions}
    result.update({"advisory_only": True, "executed": False,
                   "count": len(actions), "conflict": len(types) > 1})
    if len(actions) > 1:
        result["all"] = actions
    return result


def _public_action(act, include_arg=False):
    """ACTION для вывода: без содержимого аргумента, если нет opt-in."""
    if act is None:
        return None
    public = {"type": act["type"], "advisory_only": True, "executed": False,
              "count": act.get("count", 1), "conflict": bool(act.get("conflict"))}
    if include_arg:
        public["arg"] = act.get("arg", "")
    else:
        public["arg_chars"] = len(act.get("arg", ""))
    return public


def _utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _draft_key(p):
    # Один активный черновик на провайдера: блокируем чат целиком, чтобы
    # подготовка, вложения, текст, клик и фиксация не пересекались.
    return f"chat:{p}"


def _pending_chats(st):
    return [p for p in PROVIDERS
            if isinstance(st.get("chats", {}).get(p), dict) and st["chats"][p].get("pending_send")]


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
    with draft_lock(_draft_key(p)):
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
        method = None
        attempt = None
        pending_written = False
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
            attempt = {
                "chat": p,
                "task_id": task_id,
                "conversation_id": conversation_id,
                "prepared_id": st["chats"][p].get("prepared_id"),
                "inspected_url": st["chats"][p].get("inspected_url"),
                "at": _utc_now(),
                "stage": "requested",
                "stages": ["requested"],
                "files_requested": len(files),
                "files": [f.name for f in files],
                "files_added": 0 if files else 0,
                "method": None,
                "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "delivery": "unknown",
                "generation": GENERATION_UNKNOWN,
            }
            try:
                method = attach(page, c, files)
                stage = "attachments_added" if files else "no_attachments"
                attempt.update({"stage": stage, "method": method,
                                "files_added": len(files) if files else 0})
                attempt["stages"].append(stage)
                put_text(page, c, text.strip())
                attempt.update({"stage": "text_ready"})
                attempt["stages"].append("text_ready")
                # Persist uncertainty BEFORE the click: a crash/timeout cannot silently retry.
                before_send = {key: st["chats"][p].get(key) for key in ("manifest_sent", "ref_sent", "new_task_pending", "messages")}
                ref_included = bool(ref_img and any(f.resolve() == Path(ref_img).resolve() for f in files))
                attempt.update({"stage": "send_started"})
                attempt["stages"].append("send_started")
                _update_context(p, task_id, conversation_id, {"pending_send": True, "pending_previous": before_send,
                                "pending_context": attempt, "inspected_url": None,
                                "pending_manifest": with_manifest, "pending_ref": ref_included}, require_no_pending=True)
                pending_written = True
                click_send(page, c)
                attempt.update({"stage": "sent_unknown", "delivery": "unknown",
                                "generation": GENERATION_UNKNOWN})
                attempt["stages"].append("sent_unknown")
                _update_context(p, task_id, conversation_id, {"pending_send": True, "pending_context": attempt,
                    "new_task_pending": False,
                    "manifest_sent": bool(with_manifest or st["chats"][p].get("manifest_sent")),
                    "ref_sent": bool(st["chats"][p].get("ref_sent") or (ref_img and any(f.resolve() == Path(ref_img).resolve() for f in files)))})
                reply = wait_reply(page, c, before, a.timeout or int(BR.get("reply_timeout_seconds", 600)))
                generation = getattr(reply, "generation", GENERATION_UNKNOWN)
                url = page.url
            except Exception:
                if pending_written:
                    # Click could have happened (or the outcome is still unknown):
                    # keep pending and record the uncertain stage, never "not sent".
                    try:
                        attempt.update({"stage": "sent_unknown", "delivery": "unknown",
                                        "generation": GENERATION_UNKNOWN})
                        if attempt["stages"][-1] != "sent_unknown":
                            attempt["stages"].append("sent_unknown")
                        _update_context(p, task_id, conversation_id, {"pending_send": True, "pending_context": attempt})
                    except ChatError:
                        pass
                elif attempt is not None:
                    try:
                        if attempt["stage"] == "requested":
                            failed_stage = "add_failed"
                        elif attempt["stage"] in ("attachments_added", "no_attachments"):
                            failed_stage = "text_failed"
                        else:
                            failed_stage = "pre_send_failed"
                        attempt.update({"stage": failed_stage, "delivery": "not_sent"})
                        attempt["stages"].append(failed_stage)
                        _update_context(p, task_id, conversation_id, {"last_attachment": attempt})
                    except ChatError:
                        pass
                # Keep pending; preserve the selected URL if still on the same task.
                try:
                    if _valid_target(page.url, c["host"]):
                        _update_context(p, task_id, conversation_id, {"url": page.url})
                except ChatError:
                    pass
                raise
        changes = {"pending_send": False, "pending_previous": None, "pending_context": None,
                   "pending_manifest": False, "pending_ref": False, "new_task_pending": False,
                   "messages": int(st["chats"][p].get("messages", 0)) + 1,
                   "last_attachment": dict(attempt, stage="reply_received", delivery="unverified",
                                           generation=generation,
                                           stages=attempt["stages"] + ["reply_received"])}
        if _valid_target(url, c["host"]):
            changes["url"] = url
        _update_context(p, task_id, conversation_id, changes)
        # Журнал по умолчанию — только метаданные. Содержимое сохраняется
        # исключительно при явном --log-content. Имя уникально и создаётся
        # атомарно: параллельные отправки не перезаписывают чужие журналы.
        if getattr(a, "log_content", False):
            log = (f"# SENT ({method}, files={len(files)})\n\n{text}\n\n# REPLY ({generation})\n\n{reply}\n")
        else:
            log = (f"# Chat metadata\nprovider={p}\nattachment_method={method}\n"
                   f"files_requested={len(files)}\nattachment_delivery=unverified\n"
                   f"generation={generation}\ncontent_saved=false\n")
        lp = create_unique_file(log_dir("chats"), p, ".md", log)
        act = parse_action(reply)
        show_content = bool(getattr(a, "show_content", False))
        summary = (f"=== REPLY {p} | manifest={'yes' if with_manifest else 'no'} | attach={method} | "
                   f"files_requested={len(files)} | attachment_delivery=unverified | "
                   f"generation={generation} | log={lp} ===\n"
                   f"reply_chars={len(reply)} reply_sha256={hashlib.sha256(str(reply).encode('utf-8')).hexdigest()}")
        if show_content:
            summary += f"\n{reply}"
        else:
            summary += "\n[содержимое ответа не выводится; используйте --show-content]"
        summary += f"\n=== PARSED ACTION (advisory only; not executed): " \
                   f"{json.dumps(_public_action(act, include_arg=show_content), ensure_ascii=False)} ==="
        out(summary)


def cmd_read(a):
    p = a.to or CFG["chats"]["primary"]
    c = provider_cfg(p)
    with Session() as s:
        page = get_page(s, p)
        txt = last_reply_text(page, c)
        state = generation_state(page, c)
    act = parse_action(txt)
    show_content = bool(getattr(a, "show_content", False))
    generating = {GENERATION_GENERATING: True, GENERATION_FINISHED: False}.get(state, "unknown")
    lines = [f"=== LAST REPLY {p} | generating={generating} | generation={state} | chars={len(txt)} ==="]
    if state == GENERATION_UNKNOWN:
        lines.append("Состояние генерации неизвестно; ответ может быть неполным.")
    if show_content:
        lines.append(txt)
    else:
        lines.append("[содержимое ответа не выводится; используйте --show-content]")
    lines.append("=== PARSED ACTION (advisory only; not executed): "
                 f"{json.dumps(_public_action(act, include_arg=show_content), ensure_ascii=False)} ===")
    out("\n".join(lines))


def cmd_new(a):
    p = a.to
    c = provider_cfg(p)
    with draft_lock(_draft_key(p)):
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
    p = a.to
    with draft_lock(_draft_key(p)):
        st = load_state()
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
    with draft_lock(_draft_key(p)):
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
            context = ch.get("pending_context")
            if isinstance(context, dict):
                reviewed = dict(context)
                reviewed["stage"] = "operator_reviewed_sent" if a.reviewed_sent else "operator_reviewed_not_sent"
                reviewed["delivery"] = "unknown" if a.reviewed_sent else "not_sent"
                reviewed["stages"] = list(context.get("stages") or []) + [reviewed["stage"]]
                ch["last_attachment"] = reviewed
            ch.update({"pending_send": False, "pending_previous": None, "pending_context": None,
                       "pending_manifest": False, "pending_ref": False})
        update_state(mutate)
        out({"ok": True, "chat": p, "note": "Блокировка снята после проверки; автоматический повтор не выполнялся."})


def cmd_set_url(a):
    p = a.to
    c = provider_cfg(p)
    with draft_lock(_draft_key(p)):
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

    def change(cfg):
        cfg.setdefault("chats", {})["primary"] = a.provider
        return cfg

    updated = update_config(change)
    CFG["chats"]["primary"] = updated["chats"]["primary"]
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
    with ExitStack() as stack:
        for p in sorted(PROVIDERS):
            stack.enter_context(draft_lock(_draft_key(p)))

        def mutate(st):
            pending = _pending_chats(st)
            if pending:
                raise ChatError(
                    "PENDING_SEND",
                    "Есть неразрешённая отправка в чате(ах) "
                    f"{', '.join(pending)}; новая задача не создана. Сначала проверьте чат и выполните resolve.")
            st["task"] = task_text.strip()
            st["task_id"] = uuid.uuid4().hex
            st["task_context_valid"] = True
            st["ref_image"] = ref
            for p in PROVIDERS:
                if p in st.get("chats", {}):
                    st["chats"][p].update({"url": None, "manifest_sent": False, "ref_sent": False, "messages": 0,
                                           "new_task_pending": True, "prepared_id": None, "inspected_url": None,
                                           "pending_send": False, "pending_previous": None, "pending_context": None,
                                           "pending_manifest": False, "pending_ref": False, "last_attachment": None,
                                           "conversation_id": uuid.uuid4().hex})
        update_state(mutate)
    # Текст задачи хранится в state.json и уходит Стратегу, но не выводится в
    # stdout по умолчанию: он может содержать личные данные. В выводе только
    # метаданные; сам текст доступен в локальном state.json.
    out({"ok": True, "task_chars": len(task_text.strip()),
         "task_sha256": hashlib.sha256(task_text.strip().encode("utf-8")).hexdigest(),
         "content_echoed": False, "ref_image": ref,
         "note": "Следующая отправка начнёт новую беседу и получит контекст текущей задачи."})


def cmd_status(a):
    st = load_state()
    probe = cdp_probe()
    task_text = st.get("task") or ""
    res = {"browser": probe["verified"], "port": PORT, "cdp": probe,
           "primary": CFG["chats"]["primary"],
           "task_chars": len(task_text),
           "task_sha256": hashlib.sha256(task_text.encode("utf-8")).hexdigest() if task_text else None,
           "task_context_valid": bool(st.get("task_context_valid")),
           "chats": {}}
    for p in PROVIDERS:
        ch = st["chats"][p]
        if ch.get("pending_send"):
            res["chats"][p] = {"pending_send": True, "pending_context": ch.get("pending_context")}
    if res["browser"]:
        # Статус пассивен: только существующие контексты/вкладки, без
        # new_context/new_page/goto. Отсутствие вкладки — явный статус, не повод
        # её открыть.
        session = Session()
        # Флаг ставится до __enter__ и не требует аргументов конструктора:
        # статус не должен создавать новый контекст даже в вырожденном случае.
        session.create_context = False
        with session as s:
            for p in PROVIDERS:
                c = CFG["chats"][p]
                try:
                    page = get_page(s, p, create=False)
                    if page is None:
                        res["chats"].setdefault(p, {}).update(
                            {"status": "NOT_OPEN", "url": None,
                             "manifest_sent": st["chats"][p].get("manifest_sent"),
                             "note": "Вкладка не открыта; статус не создаёт вкладок."})
                        continue
                    res["chats"].setdefault(p, {}).update(
                        {"status": check_ready(page, c, wait_ms=20000), "url": page.url,
                         "manifest_sent": st["chats"][p].get("manifest_sent")})
                except Exception as e:
                    res["chats"].setdefault(p, {}).update({"status": "ERROR", "error": str(e)[:300]})
    elif probe["reachable"]:
        res["hint"] = (f"Порт {PORT} занят, но CDP не подтвердил наш Chrome/профиль "
                       f"({probe['reason']}); работа с чатами остановлена.")
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
    p.add_argument("--log-content", action="store_true", help="явно сохранить полный текст сообщения (включая задачу/манифест) и ответа в локальный журнал; журнал может содержать личные данные")
    p.add_argument("--show-content", action="store_true", help="явно вывести текст ответа в stdout; по умолчанию выводятся только метаданные")
    p.add_argument("--confirmed-no-prior-attachments", action="store_true", help="только после осмотра текущей вкладки: в редакторе нет чужих вложений")
    p = sp.add_parser("read"); p.add_argument("--to", choices=PROVIDERS)
    p.add_argument("--show-content", action="store_true", help="явно вывести текст последнего ответа в stdout; по умолчанию только метаданные")
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
