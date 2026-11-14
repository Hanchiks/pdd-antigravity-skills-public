#!/usr/bin/env python
"""pdd.py — управление Android-телефоном (Pinduoduo) через ADB по Wi-Fi. Без ИИ.

Все координаты tap/swipe — в системе координат скриншота для Оператора
(ширина phone.view_width из config.yaml, по умолчанию 720 px).

Примеры:
  python pdd.py status
  python pdd.py connect 192.168.1.50:37123
  python pdd.py screen [--grid]
  python pdd.py ui [--find 收藏] [--all]
  python pdd.py tap 360 1200        |  python pdd.py tapel 12
  python pdd.py scroll down [--amount 0.6]
  python pdd.py text "羽绒服 男" [--enter]
  python pdd.py back | home | launch | wait 3
  python pdd.py dossier img1.jpg img2.jpg ... [--labels "Карточка|SKU|Отзывы"] [--collage]
"""
import argparse
import base64
import hashlib
import io
import json
import math
import os
import random
import re
import subprocess
import sys
import time
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from common import (ROOT, StateError, device_lock, load_config, load_state, log_dir, out,
                    update_config, update_state)

CFG = load_config()
PH = CFG["phone"]
SERIAL = None
ADBKEYBOARD_PACKAGE = "com.android.adbkeyboard"
ADBKEYBOARD_IME = ADBKEYBOARD_PACKAGE + "/.AdbIME"
ADBKEYBOARD_URL = "https://github.com/senzhk/ADBKeyBoard/raw/master/ADBKeyboard.apk"
ADBKEYBOARD_SHA256 = "e698adea5633135a067b038f9a0cf41baa4de09888713a81593fb2b9682cdc59"


class AdbError(RuntimeError):
    pass


class UiIdleError(AdbError):
    """Штатный дамп не получен из-за подтверждённой ошибки idle/таймаута."""


# ----------------------------------------------------------------- ADB basics
def run_adb(args, use_serial=True, binary=False, timeout=40, check=True, include_stderr=False,
            sensitive=False, redact=()):
    """Запуск adb.

    ``sensitive`` скрывает всю командную строку в сообщении о таймауте.
    ``redact`` заменяет перечисленные значения (текст, код сопряжения, base64)
    на ``***`` в сообщении об ошибке, если adb успел вернуть ненулевой код.
    Это снижает риск утечки, но не скрывает argv от ОС, adb-сервера и журналов
    устройства: они остаются вне контроля этого процесса.
    """
    cmd = [PH.get("adb_path") or "adb"]
    if use_serial and SERIAL:
        cmd += ["-s", SERIAL]
    cmd += [str(a) for a in args]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        if sensitive:
            raise AdbError("Таймаут команды: аргументы скрыты, так как могут содержать секрет")
        shown = " ".join(
            "[скрыто]" if any(secret and str(arg) == str(secret) for secret in redact) else str(arg)
            for arg in cmd)
        raise AdbError(f"Таймаут команды: {shown}")
    if check and r.returncode != 0:
        msg = (r.stderr or r.stdout).decode("utf-8", errors="replace").strip()
        for secret in redact:
            if secret:
                msg = msg.replace(str(secret), "***")
        if "INJECT_EVENTS" in msg:
            msg += ("\nПОДСКАЗКА: на Xiaomi/Redmi/POCO включите «Отладка по USB (настройки безопасности)» "
                    "в «Для разработчиков», иначе тапы запрещены.")
        raise AdbError(msg or f"adb вернул код {r.returncode}")
    output = r.stdout
    if include_stderr and r.stderr:
        output += (b"\n" if output else b"") + r.stderr
    return output if binary else output.decode("utf-8", errors="replace")


def _parse_devices(txt: str):
    devs = []
    for line in txt.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            devs.append((parts[0], parts[1]))
    return devs


def list_devices(with_mdns=False):
    if with_mdns:
        run_adb(["mdns", "services"], use_serial=False, check=False)
        time.sleep(1.0)
    txt = run_adb(["devices"], use_serial=False, check=False)
    devs = _parse_devices(txt)
    if not devs and not with_mdns:
        run_adb(["mdns", "services"], use_serial=False, check=False)
        time.sleep(1.0)
        txt = run_adb(["devices"], use_serial=False, check=False)
        devs = _parse_devices(txt)
    return devs


def ensure_device():
    """Находит/переподключает телефон без лишних задержек, если уже подключён. Возвращает serial."""
    global SERIAL
    want = (PH.get("serial") or "").strip()
    devs = list_devices(with_mdns=False)
    ok = [d for d, s in devs if s == "device"]
    if want:
        if want in ok:
            SERIAL = want
            return want
        if ":" in want:
            run_adb(["connect", want], use_serial=False, check=False, timeout=15)
            time.sleep(0.5)
            ok = [d for d, s in list_devices(with_mdns=True) if s == "device"]
        if want not in ok:
            raise AdbError(
                f"Телефон {want} не подключён. Проверьте, что включена «Беспроводная отладка» и что телефон "
                f"и ПК в одной сети Wi-Fi. Порт меняется после перезагрузки/переподключения Wi-Fi — "
                f"посмотрите новый IP:порт в настройках беспроводной отладки и выполните: python pdd.py connect IP:PORT"
            )
        SERIAL = want
        return want
    if len(ok) == 1:
        SERIAL = ok[0]
        return SERIAL
    if not ok:
        raise AdbError("Нет подключённых устройств. Выполните: python pdd.py connect IP:PORT "
                       "(IP:порт — в «Для разработчиков → Беспроводная отладка»). "
                       "Если ПК ещё не сопряжён: python pdd.py pair IP:PAIRPORT CODE")
    raise AdbError(f"Подключено несколько устройств {ok}. Укажите phone.serial в config.yaml или python pdd.py connect IP:PORT")


def pace():
    a, b = PH.get("pace_seconds", [1.0, 2.0])
    time.sleep(random.uniform(a, b))


def _parse_wm_size(txt):
    m = re.search(r"Override size:\s*(\d+)x(\d+)", txt) or re.search(r"Physical size:\s*(\d+)x(\d+)", txt)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def screen_info(refresh=False):
    st = load_state()
    sc = st.get("screen") or {}
    if sc.get("serial") == SERIAL and sc.get("real_w") and not refresh:
        return sc
    txt = run_adb(["shell", "wm", "size"])
    size = _parse_wm_size(txt)
    if not size:
        raise AdbError(f"Не удалось определить размер экрана: {txt}")
    rw, rh = size
    vw = int(PH.get("view_width", 720))
    scale = rw / vw
    sc = {"serial": SERIAL, "real_w": rw, "real_h": rh, "view_w": vw, "view_h": round(rh / scale),
          "scale": scale,
          # Ориентация выводится только из соотношения сторон: поворот на 180°
          # при тех же размерах этим не обнаруживается.
          "orientation": "portrait" if rh >= rw else "landscape",
          "captured_at": time.time()}
    update_state(lambda state: state.update({"screen": sc}))
    return sc


def _invalidate_screen_cache():
    def clear(state):
        state.pop("screen", None)
    update_state(clear)


def _require_fresh_geometry():
    """Сверяет кеш геометрии с фактическим wm size перед действием.

    При расхождении кеш инвалидируется и действие отменяется: старые
    координаты view нельзя молча применять к новому экрану. Надёжного
    источника поворота на 180° среди используемых ADB-команд нет, поэтому
    сравниваются размеры/соотношение w/h (см. ограничение в README).
    """
    stale = load_state().get("screen") or {}
    try:
        fresh = screen_info(refresh=True)
    except AdbError:
        _invalidate_screen_cache()
        raise
    if stale.get("serial") != SERIAL:
        return fresh
    old = (stale.get("real_w"), stale.get("real_h"))
    new = (fresh.get("real_w"), fresh.get("real_h"))
    if old != new:
        _invalidate_screen_cache()
        raise AdbError(
            f"Геометрия экрана изменилась: кеш {old[0]}x{old[1]}, сейчас {new[0]}x{new[1]}. "
            "Кеш геометрии сброшен; действие не выполнялось. Получите свежий экран "
            "(python pdd.py screen) или дерево (python pdd.py ui) и повторите.")
    return fresh


def to_real(x, y):
    sc = screen_info()
    return round(float(x) * sc["scale"]), round(float(y) * sc["scale"])


def to_view(x, y):
    sc = screen_info()
    return round(x / sc["scale"]), round(y / sc["scale"])


# ----------------------------------------------------------------- Screenshots
def _font(size):
    for name in ("msyh.ttc", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            pass
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def draw_grid(img: Image.Image) -> Image.Image:
    img = img.copy().convert("RGBA")
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    f = _font(13)
    w, h = img.size
    for x in range(0, w, 50):
        major = x % 100 == 0
        d.line([(x, 0), (x, h)], fill=(255, 0, 0, 75 if major else 30), width=1)
        if major and x:
            for yy in (2, h - 18):
                d.rectangle([x + 1, yy, x + 32, yy + 15], fill=(255, 255, 255, 210))
                d.text((x + 3, yy + 1), str(x), fill=(200, 0, 0, 255), font=f)
    for y in range(0, h, 50):
        major = y % 100 == 0
        d.line([(0, y), (w, y)], fill=(0, 0, 255, 75 if major else 30), width=1)
        if major and y:
            for xx in (2, w - 38):
                d.rectangle([xx, y + 1, xx + 36, y + 16], fill=(255, 255, 255, 210))
                d.text((xx + 2, y + 2), str(y), fill=(0, 0, 200, 255), font=f)
    return Image.alpha_composite(img, layer).convert("RGB")


def capture(check_geometry=True):
    png = run_adb(["exec-out", "screencap", "-p"], binary=True, timeout=30)
    if not png.startswith(b"\x89PNG"):
        raise AdbError("screencap вернул не PNG (экран заблокирован или защищён от записи?)")
    img = Image.open(io.BytesIO(png)).convert("RGB")
    if check_geometry:
        sc = load_state().get("screen") or {}
        if (sc.get("serial") == SERIAL and sc.get("real_w") and sc.get("real_h")
                and img.size != (sc["real_w"], sc["real_h"])):
            _invalidate_screen_cache()
            raise AdbError(
                f"Размер скриншота {img.width}x{img.height} не совпадает с кешированной геометрией "
                f"{sc['real_w']}x{sc['real_h']}. Кеш геометрии сброшен; снимок не использован. "
                "Получите свежий экран: python pdd.py screen (и при необходимости ui).")
    return img


def cmd_screen(a):
    ensure_device()
    # screen явно переустанавливает геометрию, поэтому строгая сверка capture
    # здесь не применяется: несовпадение обрабатывается ниже.
    img = capture(check_geometry=False)
    rw, rh = img.size
    sc = screen_info()
    if (rw, rh) != (sc["real_w"], sc["real_h"]):
        sc = screen_info(refresh=True)
        if (rw, rh) != (sc["real_w"], sc["real_h"]):  # повёрнут экран и т.п.
            vw = int(PH.get("view_width", 720))
            sc = {"serial": SERIAL, "real_w": rw, "real_h": rh, "view_w": vw,
                  "view_h": round(rh * vw / rw), "scale": rw / vw}
            update_state(lambda state: state.update({"screen": sc}))
    def next_shot(state):
        state["shot"] = int(state.get("shot", 0)) + 1
        return state["shot"]
    n = update_state(next_shot)
    d = log_dir("screens")
    view = img.resize((sc["view_w"], sc["view_h"]), Image.LANCZOS)
    vpath = d / f"s{n:04d}_view.jpg"
    (draw_grid(view) if a.grid else view).save(vpath, quality=85)
    sw = min(int(PH.get("send_width", 1080)), rw)
    send = img.resize((sw, round(rh * sw / rw)), Image.LANCZOS) if sw != rw else img
    spath = d / f"s{n:04d}_send.jpg"
    send.save(spath, quality=88)
    out({"view": str(vpath), "send": str(spath), "view_size": [sc["view_w"], sc["view_h"]],
         "grid": bool(a.grid),
         "hint": "Координаты tap — по сетке 'view' (720x1600): шапка y=110 (< Назад x=50); низ карточки y=1475 (店铺 x=60, 收藏 x=135, 客服 x=215, 发起拼单 x=630). Стратегу отправляйте 'send'."})


# ----------------------------------------------------------------- UI tree
class UiDump(str):
    """XML с указанием, скрывал ли uiautomator часть узлов."""
    def __new__(cls, xml, compressed=False):
        obj = super().__new__(cls, xml)
        obj.compressed = compressed
        return obj


class UiElements(list):
    def __init__(self, elements, compressed=False):
        super().__init__(elements)
        self.compressed = compressed


def _detect_live_player(img):
    """Распознаёт только наблюдавшийся мини-плеер POCO; при сомнении отказ."""
    if img.size != (1080, 2400):
        return False
    view = img.convert("RGB").resize((720, 1600), Image.Resampling.LANCZOS)
    pixel = view.load()
    def white(p):
        return min(p) >= 185 and max(p) - min(p) < 70
    def red(p):
        r, g, b = p
        return r > 140 and r > 1.5 * g and r > 1.3 * b

    # Белый X и тёмная подложка: одни лишь координаты не являются признаком.
    for sign in (-1, 1):
        if sum(white(pixel[681 + d, 361 + sign * d]) for d in range(-8, 9)) < 15:
            return False
    pad = [pixel[x, y] for x in range(668, 695) for y in range(348, 375)]
    if sum(max(p) < 155 for p in pad) < len(pad) * .65:
        return False
    # Отдельная красная плашка под видео и белый индикатор/надпись на ней.
    banner = [pixel[x, y] for x in range(552, 696) for y in range(576, 599)]
    if sum(red(p) for p in banner) < len(banner) * .65:
        return False
    for left, right, minimum in ((582, 610, 35), (610, 660, 100), (660, 680, 12)):
        if sum(white(pixel[x, y]) for x in range(left, right) for y in range(579, 598)) < minimum:
            return False
    return True


def _require_player_touch_window(rx, ry):
    """Дополнительно проверяет получателя касания и окна над ним."""
    import input_window_guard
    package = PH.get("package") or ""
    if package != "com.xunmeng.pinduoduo":
        raise AdbError("Неизвестный пакет для автоматического закрытия плеера.")
    windows = run_adb(["shell", "dumpsys", "window"], timeout=15)
    focus = re.findall(r"^\s*mCurrentFocus\s*=\s*(.+)$", windows, re.MULTILINE)
    if len(focus) != 1:
        raise AdbError("Не удалось подтвердить точное окно Pinduoduo.")
    component = re.search(r"^Window\{([0-9a-fA-F]+)\s+u\d+\s+"
                          r"(com\.xunmeng\.pinduoduo/[A-Za-z0-9_.$]+)\}\s*$", focus[0])
    if not component:
        raise AdbError("Не удалось подтвердить точное окно Pinduoduo.")
    packages = run_adb(["shell", "cmd", "package", "list", "packages", "-U", package], timeout=10)
    uids = re.findall(r"^package:" + re.escape(package) + r"\s+uid:(\d+)\s*$", packages, re.MULTILINE)
    if len(uids) != 1:
        raise AdbError("Не удалось подтвердить владельца окна Pinduoduo.")
    dispatch = run_adb(["shell", "dumpsys", "input"], timeout=15)
    try:
        input_window_guard.require_expected_touch_window(dispatch, component.group(2), int(uids[0]), rx, ry,
                                                         window_hash=component.group(1))
    except input_window_guard.UntrustedInputWindow as exc:
        raise AdbError("Нельзя исключить перекрытие точки закрытия плеера.") from exc


def _require_player_tap_bounds(rx, ry):
    """Точка закрытия должна быть в допустимой области текущего экрана."""
    sc = screen_info()
    if not (math.isfinite(rx) and math.isfinite(ry)):
        raise AdbError("Некорректные координаты закрытия плеера; нажатие отменено.")
    if not (0 <= rx < sc["real_w"] and 88 <= ry < sc["real_h"]):
        raise AdbError("Точка закрытия плеера выходит за безопасные границы экрана; нажатие отменено.")


def _close_live_player_confirmed():
    """Один ограниченный тап только по подтверждённому визуально мини-плееру.

    Возвращает False, если подтверждённого плеера нет. Любое сомнение
    (второй кадр, отсутствие движения видео, смена фокуса, перекрытие окна)
    поднимает AdbError с причиной и не выполняет нажатие. Вызывается только
    из явной команды close-player, а не из путей чтения.
    """
    _require_pdd_focus()
    first = capture()
    if not _detect_live_player(first):
        return False
    time.sleep(0.5)
    _require_pdd_focus()
    second = capture()
    if not _detect_live_player(second):
        raise AdbError("Мини-плеер не подтверждён на втором свежем кадре; нажатие не выполнялось.")
    # Видео должно действительно меняться: статичная картинка с X и
    # похожей плашкой не даёт разрешения на нажатие.
    region = (831, 585, 990, 855)
    a = first.crop(region).resize((32, 54)).convert("RGB").tobytes()
    b = second.crop(region).resize((32, 54)).convert("RGB").tobytes()
    if sum(max(abs(a[i + j] - b[i + j]) for j in range(3)) >= 30
           for i in range(0, len(a), 3)) < 40:
        raise AdbError("Видео мини-плеера не меняется; перекрытие не подтверждено, нажатие не выполнялось.")
    _require_pdd_focus()
    _require_player_tap_bounds(1022, 542)
    _require_player_touch_window(1022, 542)
    tap_real(1022, 542)  # (681, 361) view, только проверенный крестик
    return True


def _dump_ui_attempts(compressions):
    last = ""
    idle_like = True
    # A timed-out remote process may finish later; unique paths prevent its
    # output from being mistaken for this attempt's current tree.
    for compressed in compressions:
        path = f"/sdcard/pdd_ui_{uuid.uuid4().hex}.xml"
        phase = "dump"
        try:
            args = ["shell", "uiautomator", "dump"]
            if compressed:
                args.append("--compressed")
            res = run_adb(args + [path], check=False, timeout=12, include_stderr=True)
            if "dumped to" in res:
                phase = "read"
                txt = run_adb(["exec-out", "cat", path], timeout=5)
                i, j = txt.find("<?xml"), txt.rfind("</hierarchy>")
                if i != -1 and j != -1:
                    return UiDump(txt[i:j + len("</hierarchy>")], compressed=compressed), "", False
                last = "Дамп неполный или некорректный"
                idle_like = False
            else:
                last = res.strip()[:200] or "Пустой ответ uiautomator"
                idle_like = idle_like and "could not get idle state" in res.lower()
        except AdbError as exc:
            last = str(exc)[:200]
            idle_like = idle_like and phase == "dump" and "Таймаут команды" in str(exc)
        finally:
            try:
                run_adb(["shell", "rm", "-f", path], check=False, timeout=5)
            except AdbError:
                pass
    return None, last, idle_like


def dump_ui_xml():
    """Только чтение дерева: не нажимает, не свайпает и не шлёт keyevent.

    Автоматическое закрытие мини-плеера здесь запрещено: путь чтения обязан
    оставаться пассивным даже при ошибках и повторах. Для закрытия перекрытия
    есть отдельная явная команда close-player.
    """
    xml, last, idle_like = _dump_ui_attempts((False,))
    if xml is not None:
        return xml
    # Сжатый дамп предназначен для просмотра; read_ui_elements(require_full=True)
    # по-прежнему не допускает нажатия на его основании.
    xml, last, compressed_idle = _dump_ui_attempts((True,))
    if xml is not None:
        return xml
    idle_like = idle_like and compressed_idle
    player_detail = ("Дерево UI недоступно; автоматических действий (tap/swipe/keyevent) не выполнялось. "
                     "Для закрытия перекрытия используйте явную команду close-player. ")
    if "could not get idle state" in last.lower():
        reason = "Android сообщает: could not get idle state."
    elif "Таймаут команды" in last:
        reason = "Истекло время ожидания Android."
    elif "неполный или некорректный" in last:
        reason = "Получено неполное или некорректное дерево."
    else:
        reason = "Android не вернул пригодное дерево."
    error_cls = UiIdleError if idle_like else AdbError
    raise error_cls("uiautomator dump не удался; нажатия заблокированы до получения актуального дерева UI. "
                    + player_detail + "Проверьте состояние экрана и повторите. " + reason)


SPEC_KEYS = {
    "面料", "材质", "面料/材质", "成分含量", "主面料成分", "面料成分",
    "裤型", "款式", "版型", "腰型", "裤长", "厚薄", "工艺", "品牌", "适用季节", "风格"
}


def _build_ui_digest(els):
    notes = []
    card_sales, shop_sales, brand_sales, unknown_sales = [], [], [], []
    has_card_rev, has_shop_rev = False, False
    has_details, has_comp = False, False
    detail_headers = [e for e in els if e.get("label", "").strip() in ("商品详情", "商品参数", "商品参数详情")]
    specs = []
    for i, e in enumerate(els):
        lbl = e["label"].strip()
        if "商品详情" in lbl or "商品参数" in lbl:
            has_details = True
        if ("已拼" in lbl or "已抢" in lbl) and len(lbl) < 40:
            if "品牌" in lbl:
                brand_sales.append(f"[{i}] {lbl} (销量属于品牌)")
            elif "本店" in lbl or "店铺" in lbl:
                shop_sales.append(f"[{i}] {lbl} (⚠️ ПРОДАЖИ МАГАЗИНА, НЕ карточки!)")
            elif any(x in lbl for x in ("本商品已拼", "商品已拼", "本商品已抢", "商品已抢")):
                card_sales.append(f"[{i}] {lbl} (указано, что это товар)")
            else:
                unknown_sales.append(f"[{i}] {lbl} (принадлежность неясна; проверить контекст на экране)")
        if "商品评价" in lbl or "商品晒单" in lbl:
            has_card_rev = True
            notes.append(f"✅ Отзывы карточки: [{i}] {lbl}")
        if "店铺评价" in lbl or "进店看看" in lbl:
            has_shop_rev = True
        if lbl in SPEC_KEYS and detail_headers:
            # Только явный блок параметров и соседние узлы одной строки.
            # Одна лишь близость по координатам связывает фильтры выдачи
            # («品牌», «筛选») и другие несвязанные элементы.
            path = e.get("tree_path", "")
            parent = path.rpartition("/")[0] if "/" in path else ""
            in_section = any(h.get("package") == e.get("package")
                             and 0 < e["vy"] - h["vy"] <= 1000 for h in detail_headers)
            if not parent or not in_section or e.get("click"):
                continue
            vals = []
            for e2 in els:
                text = e2.get("label", "").strip()
                other_path = e2.get("tree_path", "")
                if (e2 is e or not text or text in SPEC_KEYS
                        or text in {"筛选", "搜索", "商品详情", "商品参数", "查看全部", "更多", "确定"}
                        or e2.get("click") or e2.get("placeholder")
                        or e2.get("package") != e.get("package")
                        or "/" not in other_path or other_path.rpartition("/")[0] != parent
                        or abs(e2["vy"] - e["vy"]) > 22
                        or not 0 < e2["vx"] - e["vx"] <= 420):
                    continue
                vals.append(text)
            # Несколько подписей на одной строке неоднозначны.
            if len(vals) == 1:
                specs.append(f"{lbl}: {vals[0]}")
                if lbl == "成分含量":
                    has_comp = True
    if card_sales:
        notes.append("Продажи карточки: " + "; ".join(card_sales))
    if shop_sales:
        notes.append("Продажи магазина: " + "; ".join(shop_sales))
    if brand_sales:
        notes.append("Продажи бренда: " + "; ".join(brand_sales))
    if unknown_sales:
        notes.append("Продажи, требующие проверки: " + "; ".join(unknown_sales))
    if has_shop_rev and not has_card_rev:
        notes.append("⚠️ ВНИМАНИЕ: на экране '店铺评价' (отзывы МАГАЗИНА), а НЕ '商品评价' (отзывы карточки)! У этой карточки может быть 0 отзывов.")
    if specs:
        notes.append("📝 Параметры — заявлено продавцом (商品详情): " + " | ".join(specs))
    if has_details and not has_comp:
        notes.append("⚠️ ВНИМАНИЕ: состав '成分含量' не удалось однозначно сопоставить со значением. Проверьте параметры на экране.")
    return notes


def read_ui_elements(find=None, include_all=False, require_full=False):
    """Возвращает текущее дерево, не изменяя сохранённый набор индексов."""
    sc = screen_info()
    try:
        xml = dump_ui_xml()
        compressed = getattr(xml, "compressed", None)
        if compressed is not False and require_full:
            if compressed is True:
                raise AdbError("Получен только сжатый дамп UI: часть элементов может отсутствовать. "
                               "Нажатие заблокировано; закройте плавающее видео и повторите.")
            raise AdbError("Происхождение дерева UI не подтверждено; нажатие заблокировано.")
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise AdbError(f"Некорректное дерево UI; нажатие отменено: {e}") from e
    packages = {node.get("package") for node in root.iter("node") if node.get("package")}
    if PH.get("package") and PH["package"] not in packages:
        raise AdbError("Pinduoduo не обнаружен в актуальном дереве UI; нажатие отменено.")
    els = []
    paths = {}
    def record_paths(node, path):
        paths[id(node)] = path
        for i, child in enumerate(node):
            record_paths(child, f"{path}/{i}")
    record_paths(root, "0")
    for node in root.iter("node"):
        text = (node.get("text") or "").strip()
        desc = (node.get("content-desc") or "").strip()
        rid = (node.get("resource-id") or "").split("/")[-1]
        click = node.get("clickable") == "true"
        m = re.fullmatch(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", (node.get("bounds") or "").replace(" ", ""))
        if not m:
            if click or any(fb in val for val in (text, desc) for fb in FORBIDDEN_LABELS | GROUP_JOIN_LABELS):
                raise AdbError("Нельзя определить границы кликабельного или запрещённого элемента; нажатия заблокированы.")
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        if x2 < x1 or y2 < y1:
            if click or any(fb in val for val in (text, desc) for fb in FORBIDDEN_LABELS | GROUP_JOIN_LABELS):
                raise AdbError("Некорректный порядок границ кликабельного или запрещённого элемента; нажатия заблокированы.")
            continue
        if x2 == x1 or y2 == y1 or x2 <= 0 or y2 <= 0 or x1 >= sc["real_w"] or y1 >= sc["real_h"]:
            continue
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(sc["real_w"], x2), min(sc["real_h"], y2)
        if x2 <= x1 or y2 <= y1:
            continue
        is_edit = (node.get("class") == "android.widget.EditText" or node.get("editable") == "true") and node.get("enabled", "true") == "true"
        if not (text or desc or is_edit or (include_all and click)):
            continue
        label = text or desc or (f"<{node.get('class', '').split('.')[-1] or rid}>" if is_edit else f"<{rid or node.get('class', '').split('.')[-1]}>")
        if find and find not in label and find not in desc and find not in rid:
            continue
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        els.append({"label": label[:120], "desc": desc[:80] if desc and desc != label else "",
                    "rx": cx, "ry": cy, "vx": round(cx / sc["scale"]), "vy": round(cy / sc["scale"]),
                    "vw": round((x2 - x1) / sc["scale"]), "vh": round((y2 - y1) / sc["scale"]), "click": click,
                    "rid": rid, "class": node.get("class") or "", "placeholder": not (text or desc or is_edit),
                    "editable": is_edit, "tree_path": paths[id(node)], "package": node.get("package") or "",
                    "forbidden": any(fb in val for val in (text, desc) for fb in FORBIDDEN_LABELS),
                    "group_join_forbidden": any(fb in val for val in (text, desc) for fb in GROUP_JOIN_LABELS)})
    if not els:
        raise AdbError("Дерево UI пусто или недоступно; нажатие не выполняется.")
    return UiElements(els, compressed=compressed)


def ui_signature(els):
    """Хеш только для сравнения: полный повторный дамп остаётся в памяти."""
    return hashlib.sha256(json.dumps(els, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


UI_MAX_AGE_SECONDS = 180
PLAYER_CLOSE_TREE_MAX_AGE_SECONDS = 15
_PAGE_ANCHOR_IDS = frozenset({"goods_title", "goods_name", "product_title", "product_name",
                              "mall_name", "shop_name", "store_name", "tv_title"})


def _ui_page_anchors(els):
    """Явно названные идентификаторы заголовка товара или магазина, если они есть.

    Наличие таких узлов не гарантировано и само по себе не доказывает, что это
    та же карточка. Динамические ленты, цены и таймеры не используются.
    """
    return sorted({(e.get("rid", "").lower(), e.get("label", ""), e.get("desc", ""))
                   for e in els
                   if e.get("package") == PH.get("package") and not e.get("placeholder")
                   and e.get("rid", "").lower() in _PAGE_ANCHOR_IDS
                   and (e.get("rid", "").lower() != "tv_title"
                        or e.get("class") == "android.view.ViewGroup")
                   and e.get("label", "")})


def _require_fresh_ui(st):
    captured = st.get("ui_captured_at")
    if (isinstance(captured, bool) or not isinstance(captured, (int, float))
            or not math.isfinite(captured)):
        raise AdbError("Нет времени получения дерева UI; повторите python pdd.py ui.")
    age = time.time() - captured
    if not 0 <= age <= UI_MAX_AGE_SECONDS:
        raise AdbError("Дерево UI устарело или время системы изменилось; повторите python pdd.py ui.")


def cmd_ui(a):
    # Любая новая попытка чтения делает прежние индексы недействительными,
    # даже если устройство, обычный дамп или диагностика недоступны.
    def invalidate(state):
        for key in ("ui", "ui_signature", "ui_serial", "ui_captured_at", "ui_page_anchors", "ui_quality"):
            state.pop(key, None)
    update_state(invalidate)
    ensure_device()
    # Индексы сохраняются вместе с отметкой устройства. При нажатии полное
    # дерево считывается заново, независимо от фильтра отображения.
    captured = time.time()
    try:
        full = read_ui_elements(include_all=True)
    except UiIdleError:
        out("Штатный дамп недоступен из-за idle; показано диагностическое дерево только для просмотра. "
            "Индексы не сохранены; tap/tapel по этому дереву запрещены.")
        # Диагностический вывод всегда без локального журнала; он не содержит
        # индексов и не сохраняется в state, включая при вызове с --no-log.
        return cmd_ui_diagnostic(argparse.Namespace(find=getattr(a, "find", None), all_packages=False))
    els = full if a.all else [e for e in full if not e["placeholder"]]
    if a.find:
        # Извлечь с тем же фильтром, что и прежде, без повторного дампа.
        els = [e for e in els if a.find in e["label"] or a.find in e["desc"] or a.find in e["rid"]]
    signature = ui_signature(full)
    anchors = _ui_page_anchors(full)
    compressed = getattr(full, "compressed", None)
    quality = "compressed" if compressed is True else "full" if compressed is False else "unknown"
    update_state(lambda state: state.update({"ui": els, "ui_signature": signature, "ui_serial": SERIAL,
                                             "ui_captured_at": captured, "ui_page_anchors": anchors,
                                             "ui_quality": quality}))
    digest = _build_ui_digest(els)
    digest_txt = ("=== АВТО-СВОДКА ЭКРАНА ===\n" + "\n".join(f"• {d}" for d in digest) + "\n==========================\n") if digest else ""
    lines = [f"[{i}] {e['label']}" + (f" ({e['desc']})" if e["desc"] else "") +
             f" @({e['vx']},{e['vy']}) {e['vw']}x{e['vh']}" + (" click" if e["click"] else "")
             for i, e in enumerate(els)]
    if getattr(a, "no_log", False):
        destination = ("Копия latest.txt не записана; "
                       + ("состояние для tapel сохраняется" if quality == "full" else
                          "индексы недоступны для tapel из-за качества дерева")
                       + "; временный дамп создаётся на телефоне.")
    else:
        p = log_dir("ui") / "latest.txt"
        p.write_text(digest_txt + "\n".join(lines), encoding="utf-8")
        destination = f"Копия: {p}"
    warning = ("⚠️ Сжатый дамп может скрывать элементы; для нажатий необходимо полное дерево.\n"
               if quality == "compressed" else
               "⚠️ Качество дампа неизвестно; нажатия по индексам запрещены.\n"
               if quality == "unknown" else "")
    out(f"{warning}{digest_txt}# {len(els)} элементов (координаты view). {destination}\n" + "\n".join(lines))


def _diagnostic_focus():
    """Возвращает точную идентичность обычного активного окна PDD."""
    windows = run_adb(["shell", "dumpsys", "window"], timeout=15)
    focus = re.findall(r"^\s*mCurrentFocus\s*=\s*(.+)$", windows, re.MULTILINE)
    if len(focus) != 1:
        raise AdbError("Не удалось однозначно подтвердить окно для диагностики.")
    match = re.fullmatch(r"Window\{([0-9a-fA-F]+)\s+u(\d+)\s+"
                         r"(com\.xunmeng\.pinduoduo/[A-Za-z0-9_.$]+)\}\s*", focus[0])
    if not match or PH.get("package") != "com.xunmeng.pinduoduo":
        raise AdbError("Диагностика доступна только при подтверждённом обычном окне Pinduoduo.")
    return match.groups()


def cmd_ui_diagnostic(a):
    """Экспериментальный просмотр без ожидания idle, без сохранения индексов."""
    import ui_diagnostic
    # Диагностическое дерево нельзя соотнести с индексами прошлого экрана;
    # сбрасываем их даже при последующей ошибке или смене окна.
    def invalidate(state):
        for key in ("ui", "ui_signature", "ui_serial", "ui_captured_at", "ui_page_anchors", "ui_quality"):
            state.pop(key, None)
    update_state(invalidate)
    ensure_device()
    initial_focus = _diagnostic_focus()
    for attempt in range(1, 4):
        if _diagnostic_focus() != initial_focus:
            raise AdbError("Окно изменилось во время диагностики; результат отброшен.")
        root = ui_diagnostic.read_no_idle(run_adb, ROOT / "tools" / "no-idle-dump.jar", AdbError)
        if _diagnostic_focus() != initial_focus:
            raise AdbError("Окно изменилось во время диагностики; результат отброшен.")
        if any(node.get("package") == PH["package"] for node in root.iter("node")):
            break
    else:
        raise AdbError("Pinduoduo отсутствует в диагностическом дереве после 3 попыток; просмотр отменён.")
    try:
        lines = ui_diagnostic.format_nodes(root, PH["package"], a.find, a.all_packages)
    except ValueError as exc:
        raise AdbError(str(exc)) from exc
    out("⚠️ Экспериментальный просмотр: дерево может быть неполным или измениться при чтении. "
        "Нажатия по нему запрещены; индексы и журнал не сохраняются. "
        "Bounds указаны в реальных пикселях, не в координатах view.\n" +
        f"# {len(lines)} элементов; попытка {attempt} из 3\n" + "\n".join(lines))


def cmd_ui_short_idle(a):
    """Только просмотр: короткое ожидание не подтверждает полноту дерева."""
    import ui_diagnostic
    import ui_short_idle
    # Диагностический экран не имеет безопасных индексов; исключить также
    # случайное использование индексов, оставшихся от предыдущего экрана.
    def invalidate(state):
        for key in ("ui", "ui_signature", "ui_serial", "ui_captured_at", "ui_page_anchors", "ui_quality"):
            state.pop(key, None)
    update_state(invalidate)
    ensure_device()
    initial_focus = _diagnostic_focus()
    root = ui_short_idle.read_short_idle(run_adb, ROOT / "tools" / "short-idle-dump.jar", AdbError)
    if _diagnostic_focus() != initial_focus:
        raise AdbError("Окно изменилось во время диагностики; результат отброшен.")
    try:
        lines = ui_diagnostic.format_nodes(root, PH["package"], getattr(a, "find", None), False)
    except ValueError as exc:
        raise AdbError(str(exc)) from exc
    out("⚠️ Экспериментальный просмотр с коротким ожиданием: только активное окно; "
        "системные и другие окна не показаны. Дерево может быть неполным или измениться при чтении. "
        "Нажатия по нему запрещены; индексы и журнал не сохраняются. "
        "Bounds указаны в реальных пикселях, не в координатах view.\n" +
        f"# {len(lines)} элементов\n" + "\n".join(lines))


# ----------------------------------------------------------------- Actions
def _send_action_adb(args, **kwargs):
    """Отправка действия без слепого повтора при таймауте.

    Если ответа нет, неизвестно, дошла ли команда до устройства. Повторять
    вслепую нельзя: сначала нужно проверить экран/дерево (screen/ui).
    """
    try:
        return run_adb(args, **kwargs)
    except subprocess.TimeoutExpired as exc:
        raise AdbError(
            "Таймаут после отправки действия: unknown — неизвестно, выполнено ли действие на устройстве. "
            "Повтор вслепую запрещён; сначала проверьте экран (python pdd.py screen/ui).") from exc
    except AdbError as exc:
        if "Таймаут команды" in str(exc):
            raise AdbError(
                "Таймаут после отправки действия: unknown — неизвестно, выполнено ли действие на устройстве. "
                "Повтор вслепую запрещён; сначала проверьте экран (python pdd.py screen/ui).") from exc
        raise


def tap_real(rx, ry, long_ms=0):
    if long_ms:
        # Долгое нажатие отправлялось через `input swipe` с совпадающими
        # точками, то есть было настоящим жестом. Поэтому блокировка жестов
        # обязана перехватывать его до любого run_adb, как scroll/swipe.
        # Прежняя реализация сохранена в истории git и вернётся только после
        # отдельной приёмки жестов; обхода через env/CLI/config нет.
        _blocked_gesture("Длительное нажатие (--long)")
    _send_action_adb(["shell", "input", "tap", rx, ry])
    pace()


FORBIDDEN_LABELS = {"全选", "Выбрать всё", "去结算", "立即支付", "提交订单", "确认支付"}
# «Выбрать всё» is the Russian wording for 全选 used in rules/PDD-SECTION.md.
# The list is deliberately limited to wording documented in the rules/skill;
# it is not an exhaustive classifier of every possible UI label.
# Single-card 删除 is not in the hard set: the skill allows it only as an
# explicit, one-card operation. Mass selection/deletion is blocked separately
# below by 全选 and the left-bottom dangerous zone.
# Observed on the card next to another buyer's group; unlike 发起拼单 it
# enters an existing group and is not authorized by merely inspecting SKU.
GROUP_JOIN_LABELS = {"直接拼成"}


def _is_forbidden(e):
    return e.get("forbidden", False) or e.get("group_join_forbidden", False) or any(label in (e.get(k) or "")
               for k in ("label", "desc") for label in FORBIDDEN_LABELS | GROUP_JOIN_LABELS)


def _is_mass_selection_tree(els):
    """Признак режима массового выбора: в дереве есть 全选/«Выбрать всё»."""
    markers = ("全选", "Выбрать всё")
    return any((e.get("label") or "").startswith(markers)
               or any(marker in (e.get("desc") or "") for marker in markers)
               for e in els)


def _require_pdd_focus():
    """Не считать наличие узла PDD подтверждением активного окна."""
    windows = run_adb(["shell", "dumpsys", "window"], timeout=15)
    focus = re.findall(r"^\s*mCurrentFocus\s*=\s*(.+)$", windows, re.MULTILINE)
    if len(focus) != 1:
        raise AdbError("Нельзя однозначно определить окно на переднем плане; нажатие отменено.")
    match = re.search(r"\b([A-Za-z0-9_.]+)/[A-Za-z0-9_.$]+", focus[0])
    if match and match.group(1) == PH.get("package"):
        return
    # Android may give an application panel a PopupWindow title without a
    # component name. Verify the exact focused window's owner and parent app.
    popup = re.search(r"Window\{([0-9a-f]+)\s+u\d+\s+PopupWindow:[^}]+\}", focus[0])
    if popup:
        window_id = re.escape(popup.group(1))
        blocks = re.findall(r"^\s*Window #\d+ Window\{" + window_id +
                            r"\s+[^\n]*\}:.*?(?=^\s*Window #\d+ Window\{|\Z)",
                            windows, re.MULTILINE | re.DOTALL)
        package = re.escape(PH.get("package", ""))
        if (len(blocks) == 1 and
                re.search(r"\bmOwnerUid=\d+[^\n]*\bpackage=" + package + r"\b", blocks[0]) and
                re.search(r"\bmParentWindow=Window\{[^\n]*\s" + package + r"/", blocks[0]) and
                re.search(r"^\s*mFocusedApp=.*?\s" + package + r"/", windows, re.MULTILINE)):
            return
    raise AdbError("На переднем плане не подтверждено окно Pinduoduo; нажатие отменено.")


def _check_target(x, y, els, force=False):
    """Проверяет точку касания по свежему дереву и активному окну.

    Учитывает статус-бар, границы экрана, системные/чужие элементы,
    распознанную цель, запрещённые контейнеры и опасные зоны. Для обычного
    tap получатель касания подтверждается только активным окном PDD и
    деревом UI; разбор input-dispatch (touch-window guard) применяется
    отдельно в close-player. force не отменяет жёсткие запреты и безымянные
    слои в опасной нижней зоне. Остаточная гонка TOCTOU не устраняется.
    """
    if y < 88:
        raise AdbError("БЛОКИРОВКА КООРДИНАТ: y < 88 попадает в системный статус-бар Android.")
    sc = screen_info()
    if not (0 <= x < sc["view_w"] and 0 <= y < sc["view_h"]):
        raise AdbError("Координаты находятся за пределами экрана; нажатие отменено.")
    def covers(e):
        return (abs(x - e["vx"]) <= e.get("vw", 0) / 2 + 1
                and abs(y - e["vy"]) <= e.get("vh", 0) / 2 + 1)

    # Не нажимаем сквозь элементы другого приложения/системы, если их видно в дереве.
    for e in els:
        if e.get("package") != PH.get("package") and covers(e):
            raise AdbError("Точка перекрыта системным или неизвестным элементом; нажатие отменено.")
    recognized = [e for e in els if e.get("package") == PH.get("package")
                  and not e.get("placeholder") and (e.get("label") or e.get("desc"))
                  and e.get("vw", 0) * e.get("vh", 0) < sc["view_w"] * sc["view_h"] * 0.75
                  and covers(e)]
    if not recognized:
        raise AdbError("В точке нажатия нет распознанного элемента UI; дерево может быть неполным. Нажатие отменено.")
    # Дамп может содержать неполную WebView. По нему нельзя доказать наличие
    # всех кнопок; оператор обязан также сверить изображение экрана.
    for idx, e in enumerate(els):
        if _is_forbidden(e):
            # Даже при наличии одного дочернего узла опасной кнопки дерево
            # может не содержать другие кнопки в агрегированном контейнере.
            # Не считаем найденного потомка доказательством полноты; защищаем
            # все границы предка, если его метка содержит запрещённое действие.
            hw, hh = max(20, e.get("vw", 40) / 2), max(20, e.get("vh", 40) / 2)
            if abs(x - e["vx"]) <= hw + 1 and abs(y - e["vy"]) <= hh + 1:
                raise AdbError(f"БЛОКИРОВКА БЕЗОПАСНОСТИ: координаты попадают в запрещённую кнопку [{idx}].")
    if x < 120 and y > 1430 and any(e.get("label", "").startswith(("全选", "删除")) for e in els):
        raise AdbError("БЛОКИРОВКА БЕЗОПАСНОСТИ: опасная область массового выбора; используйте одиночные элементы после ui.")

    # Нижняя панель карточки (покупка/избранное/меню) и левый нижний угол
    # (全选/удаление) опасны: безымянный кликабельный слой здесь не позволяет
    # доказать назначение даже при --force. Слой-предок распознанной цели —
    # это обычный контейнер раскладки, его не блокируем.
    for e in els:
        if not (e.get("placeholder") and e.get("click") and covers(e)):
            continue
        path = e.get("tree_path")
        ancestor = path and any(r.get("tree_path", "").startswith(path + "/") for r in recognized)
        if not ancestor and y >= 1430:
            raise AdbError("БЛОКИРОВКА БЕЗОПАСНОСТИ: безымянный кликабельный элемент в опасной "
                           "нижней зоне; нельзя подтвердить его назначение. Нажатие отменено.")

    # Геометрия и класс не доказывают, что отдельный слой находится под
    # кнопкой. Надёжным признаком контейнера считаем только предка цели.
    if not force:
        for e in els:
            if not (e.get("placeholder") and e.get("click") and covers(e)):
                continue
            path = e.get("tree_path")
            ancestor = path and any(r.get("tree_path", "").startswith(path + "/") for r in recognized)
            if not ancestor:
                raise AdbError("В точке есть безымянный кликабельный слой; нельзя исключить перекрытие. Нажатие отменено.")


# Единый путь проверок перед действиями. Полностью объединить команды в один
# helper нельзя без изменения поведения: tap/tapel работают с координатами,
# text/key — с фокусом, close-player — с изображением и touch-window.
# Последовательность по командам:
#   tap:          ensure_device → _require_pdd_focus → свежий полный дамп →
#                 _check_target → _require_pdd_focus → input tap.
#   tapel:        ensure_device → сохранённое дерево (serial/quality/fresh/
#                 anchors) → на каждый индекс: _require_pdd_focus → свежесть →
#                 новый полный дамп → anchors + идентичность цели →
#                 _check_target → _require_pdd_focus → input tap.
#   close-player: ensure_device → _require_pdd_focus → свежий полный дамп →
#                 _require_pdd_focus → два кадра плеера, границы экрана,
#                 touch-window guard → input tap.
#   text:         ensure_device → _require_focused_editable (focus, свежий
#                 полный дамп, фокус на поле PDD, контекст поиска) → ввод;
#                 перед Enter проверка повторяется.
#   key:          ensure_device → _require_pdd_focus → свежий полный дамп
#                 (для del/search — подтверждённое поле ввода) →
#                 _require_pdd_focus → keyevent.
# Любая проверка при недоступности или неоднозначности завершает действие
# ошибкой. Проверки уменьшают, но не устраняют гонку TOCTOU между последней
# проверкой и касанием: Android может изменить окно/цель за это время.
def cmd_tap(a):
    """Координатный тап. Не подтверждает идентичность карточки/магазина/SKU.

    Для товарно-зависимых действий (收藏, удаление, выбор варианта) используйте
    tapel: он сверяет anchors страницы и идентичность элемента. Для tap такой
    сверки нет, поэтому здесь проверяются только окно, свежее дерево и
    распознанная цель в точке.
    """
    if getattr(a, "long", 0):
        # Отказ до ensure_device/_require_pdd_focus/дампа: --long — это жест,
        # и он не должен отправлять на устройство даже диагностический ADB.
        _blocked_gesture("Длительное нажатие (--long)")
    ensure_device()
    with device_lock(SERIAL):
        _require_fresh_geometry()
        _require_pdd_focus()
        els = read_ui_elements(include_all=True, require_full=True)
        _check_target(a.x, a.y, els, force=getattr(a, "force", False))
        _require_pdd_focus()
        rx, ry = to_real(a.x, a.y)
        tap_real(rx, ry, a.long)
        out({"ok": True, "action": "tap", "view": [a.x, a.y], "real": [rx, ry],
             "confirmation": "command_sent",
             "note": "Команда отправлена; изменение состояния не подтверждено. Проверьте свежий screen/ui."})


def cmd_tapel(a):
    """Тап по сохранённому индексу с повторной проверкой цели и страницы.

    Порядок: ensure_device → сохранённое дерево (серийный номер, полнота,
    свежесть, anchors) → для каждого индекса: focus → повторная свежесть →
    новый полный дамп → сверка anchors и идентичности элемента → _check_target
    → focus → tap. Это не устраняет остаточную гонку (TOCTOU) между последней
    проверкой и касанием; при сомнении действие нужно не выполнять. Успешный
    вывод означает отправленное касание, а не подтверждённое сохранение,
    удаление или выбор варианта — результат проверяется отдельным свежим ui.
    """
    if getattr(a, "long", 0):
        _blocked_gesture("Длительное нажатие (--long)")
    ensure_device()
    with device_lock(SERIAL):
        _require_fresh_geometry()
        st = load_state()
        els = st.get("ui") or []
        signature = st.get("ui_signature")
        if not signature or st.get("ui_serial") != SERIAL:
            raise AdbError("Нет актуального дерева для этого устройства; сначала выполните python pdd.py ui.")
        if st.get("ui_quality") != "full":
            raise AdbError("Исходное дерево не подтверждено как полное; повторите python pdd.py ui.")
        _require_fresh_ui(st)
        anchors = st.get("ui_page_anchors")
        if (not isinstance(anchors, list)
                or any(not isinstance(anchor, (tuple, list)) or len(anchor) != 3
                       or not all(isinstance(v, str) for v in anchor) for anchor in anchors)):
            raise AdbError("Нет данных контекста экрана; повторите python pdd.py ui.")
        indices = a.indices if isinstance(a.indices, list) else [a.indices]
        for idx in indices:
            if not (0 <= idx < len(els)):
                raise AdbError(f"Нет элемента [{idx}] — сначала выполните python pdd.py ui")
            if _is_forbidden(els[idx]):
                raise AdbError(f"БЛОКИРОВКА БЕЗОПАСНОСТИ: запрещён элемент [{idx}].")
        if len(indices) > 1:
            # Один вызов не должен отмечать несколько чекбоксов: в 全部收藏 это
            # массовый выбор, а безымянные элементы нельзя отличить от служебных
            # чекбоксов. Разрешены только явные одиночные действия.
            if any(els[idx].get("placeholder") for idx in indices):
                raise AdbError("Несколько безымянных целей за один tapel недопустимы: "
                               "это может быть массовый выбор. Нажимайте по одной.")
            if _is_mass_selection_tree(els):
                raise AdbError("На экране режим массового выбора (全选): несколько целей "
                               "за один tapel запрещены. Нажимайте по одной.")
        tapped = []
        for idx in indices:
            e = els[idx]
            try:
                _require_pdd_focus()
                _require_fresh_ui(st)
                current = read_ui_elements(include_all=True, require_full=True)
                _require_fresh_ui(st)
                if len(indices) > 1 and _is_mass_selection_tree(current):
                    raise AdbError("Экран перешёл в режим массового выбора (全选); "
                                   "несколько целей за один tapel запрещены.")
                # После сериализации JSON кортежи становятся списками.
                saved_anchors = sorted(tuple(anchor) for anchor in anchors)
                if _ui_page_anchors(current) != saved_anchors:
                    raise AdbError("Изменились доступные признаки страницы; повторите python pdd.py ui.")
                # Таймеры и ленты покупок меняются независимо от цели. Сверяем
                # сам элемент, а не хеш всего дерева; неоднозначность опасна.
                same_identity = [c for c in current
                                 if all(c.get(k) == e.get(k) for k in
                                        ("label", "desc", "rid", "placeholder", "package", "click"))]
                matches = [c for c in same_identity
                           if abs(c["vx"] - e["vx"]) <= 8 and abs(c["vy"] - e["vy"]) <= 8]
                if not matches:
                    reason = ("Элемент переместился" if same_identity
                              else "Элемент отсутствует или изменился")
                    raise AdbError(f"{reason} после ui; повторите python pdd.py ui.")
                if len(matches) > 1:
                    raise AdbError("В актуальном дереве несколько подходящих элементов; "
                                   "нажатие неоднозначно. Повторите python pdd.py ui.")
                target = matches[0]
                _check_target(target["vx"], target["vy"], current)
                _require_pdd_focus()
            except AdbError as ex:
                raise AdbError(f"Последовательность остановлена; выполнено нажатий: {len(tapped)}. {ex}") from ex
            tap_real(target["rx"], target["ry"], a.long)
            tapped.append({"index": idx, "label": target["label"], "view": [target["vx"], target["vy"]]})
        if len(tapped) == 1:
            out({"ok": True, "action": "tapel", "label": tapped[0]["label"], "view": tapped[0]["view"],
                 "confirmation": "command_sent",
                 "note": "Касание отправлено; изменение состояния не подтверждено. Проверьте свежий screen/ui."})
        else:
            out({"ok": True, "action": "tapel", "tapped": tapped, "label": tapped[-1]["label"],
                 "view": tapped[-1]["view"], "confirmation": "command_sent",
                 "note": "Касания отправлены; изменение состояния не подтверждено. Проверьте свежий screen/ui."})


def _blocked_gesture(action):
    """Исполняемая блокировка жестов до приёмки геометрии и guard'а.

    Отказ происходит до ensure_device/screen_info/run_adb. Обхода через
    env/CLI/config нет: команда сохранена только для совместимости и всегда
    завершается ошибкой.
    """
    raise AdbError(
        f"{action}: жест заблокирован до независимой приёмки источника геометрии "
        "экрана и guard'а жестов. Прокрутка, свайп и длительное нажатие запрещены. "
        "Команда не выполнялась, на устройство ничего не отправлено."
    )


def cmd_scroll(a):
    _blocked_gesture("scroll")


def cmd_swipe(a):
    _blocked_gesture("swipe")


def cmd_close_player(a):
    """Явное действие закрытия подтверждённого мини-плеера.

    Из путей чтения не вызывается. Требует свежий полный дамп UI; сохранённое
    или устаревшее дерево не используется. При неоднозначности/неподтверждении
    отказывает без нажатия. После нажатия плеер проверяется повторно: только
    фактическое отсутствие даёт state_confirmed, иначе результат unknown.
    """
    ensure_device()
    with device_lock(SERIAL):
        _require_fresh_geometry()
        _require_pdd_focus()
        started = time.time()
        read_ui_elements(include_all=True, require_full=True)
        finished = time.time()
        if not (math.isfinite(started) and math.isfinite(finished)
                and 0 <= finished - started <= PLAYER_CLOSE_TREE_MAX_AGE_SECONDS):
            raise AdbError("Дерево UI несвежее или время системы изменилось; "
                           "закрытие плеера отменено, нажатие не выполнялось.")
        _require_pdd_focus()
        if not _close_live_player_confirmed():
            out({"ok": True, "action": "close-player", "result": "not_found",
                 "player_closed": False, "confirmation": "no_action",
                 "message": "Подтверждённый мини-плеер не найден; нажатие не выполнялось."})
            return
        try:
            _require_pdd_focus()
            still_open = _detect_live_player(capture())
        except AdbError as exc:
            out({"ok": True, "action": "close-player", "result": "unknown",
                 "player_closed": False, "confirmation": "unknown",
                 "message": "Нажатие отправлено, но проверить закрытие плеера не удалось: "
                            f"{exc} Результат неизвестен; повторять вслепую нельзя, "
                            "сначала проверьте свежий screen/ui."})
            return
        if still_open:
            out({"ok": True, "action": "close-player", "result": "unknown",
                 "player_closed": False, "confirmation": "unknown",
                 "message": "Нажатие отправлено, но мини-плеер всё ещё виден; "
                            "результат не подтверждён. Не заявляйте, что плеер закрыт; "
                            "проверьте свежий screen/ui."})
            return
        out({"ok": True, "action": "close-player", "result": "closed",
             "player_closed": True, "confirmation": "state_confirmed",
             "message": "После нажатия мини-плеер повторно не обнаружен; закрытие подтверждено."})


def has_adbkeyboard():
    return ADBKEYBOARD_PACKAGE in run_adb(
        ["shell", "pm", "list", "packages", ADBKEYBOARD_PACKAGE], check=False)


def _adbkeyboard_apk_paths():
    txt = run_adb(["shell", "pm", "path", ADBKEYBOARD_PACKAGE], check=False)
    paths = []
    for line in txt.splitlines():
        line = line.strip()
        if line.startswith("package:"):
            path = line[len("package:"):].strip()
            if path:
                paths.append(path)
    return paths


def _sha256_remote_file(path):
    try:
        data = run_adb(["exec-out", "cat", path], binary=True, timeout=30)
    except AdbError:
        return None
    return hashlib.sha256(data).hexdigest()


def adbkeyboard_status():
    """Best-effort проверка установленного ADBKeyboard по закреплённому SHA-256.

    В репозитории нет закреплённого сертификата/подписи ADBKeyboard, поэтому
    доверенного значения для signer нет: выдумывать его нельзя. Вместо этого
    сравнивается содержимое установленного base.apk с уже закреплённым SHA-256
    APK. ``unknown`` означает, что пакет найден, но APK прочитать/сверить не
    удалось (split-APK, ограничения доступа, ошибка чтения). Это не является
    подтверждением подлинности, и вызывающий код обязан явно предупредить.
    """
    listed = run_adb(["shell", "pm", "list", "packages", ADBKEYBOARD_PACKAGE], check=False)
    if ADBKEYBOARD_PACKAGE not in listed:
        return {"installed": False, "authenticity": "not_installed"}
    paths = _adbkeyboard_apk_paths()
    if len(paths) != 1:
        return {"installed": True, "authenticity": "unknown",
                "reason": "не удалось однозначно определить установленный APK (split/несколько путей)"}
    path = paths[0]
    if not path.endswith("/base.apk"):
        return {"installed": True, "authenticity": "unknown",
                "reason": "установленный APK не является base.apk"}
    digest = _sha256_remote_file(path)
    if digest is None:
        return {"installed": True, "authenticity": "unknown",
                "reason": "не удалось прочитать установленный APK"}
    if digest.lower() == ADBKEYBOARD_SHA256:
        return {"installed": True, "authenticity": "authentic", "path": path}
    return {"installed": True, "authenticity": "tampered", "path": path,
            "reason": "SHA-256 установленного APK не совпал с закреплённым"}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_adbkeyboard_apk(path: Path) -> None:
    """Отказ при несовпадении хэша: файл удаляется, установка не выполняется."""
    actual = _sha256_file(path)
    if actual.lower() != ADBKEYBOARD_SHA256:
        path.unlink(missing_ok=True)
        raise AdbError("ADBKeyboard.apk не совпал по SHA-256; файл удалён, установка отменена")


def _inspect_focused_editable(expected=None, require_search=False):
    """Сверяет вводимый элемент по свежему несжатому дереву, без записи XML.

    Возвращает (identity, text): identity нужен для повторной сверки поля,
    text — фактическое содержимое поля по данным uiautomator (None, если
    атрибут отсутствует и содержимое проверить нельзя).
    """
    _require_pdd_focus()
    xml = dump_ui_xml()
    if getattr(xml, "compressed", None) is not False:
        raise AdbError("Полнота и происхождение дерева UI не подтверждены; ввод отменён.")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise AdbError("Некорректное дерево UI; Enter отменён.") from exc
    if root.tag != "hierarchy":
        raise AdbError("Неожиданный формат дерева UI; ввод отменён.")
    focused = [n for n in root.iter("node") if n.get("focused") == "true"]
    if len(focused) != 1:
        raise AdbError("Нельзя однозначно подтвердить сфокусированное поле ввода; Enter отменён.")
    node = focused[0]
    class_name = node.get("class") or ""
    if (node.get("package") != PH.get("package") or node.get("enabled") != "true"
            or not (node.get("editable") == "true" or class_name == "android.widget.EditText")
            or any(label in (node.get(key) or "") for label in FORBIDDEN_LABELS
                   for key in ("text", "content-desc"))):
        raise AdbError("Фокус не подтверждён на доступном поле ввода Pinduoduo; Enter отменён.")
    bounds = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds") or "")
    if not bounds:
        raise AdbError("Не удалось определить границы поля ввода; Enter отменён.")
    x1, y1, x2, y2 = map(int, bounds.groups())
    sc = screen_info()
    if not (0 <= x1 < x2 <= sc["real_w"] and 0 <= y1 < y2 <= sc["real_h"]):
        raise AdbError("Некорректные границы поля ввода; Enter отменён.")
    if require_search:
        # Сам факт, что поле редактируемое, не делает Enter поиском: в чате
        # продавца он способен отправить сообщение. Требуем отдельный явный
        # признак поиска у самого поля либо кнопку поиска на той же строке.
        # Содержимое поля и content-desc могут быть введены пользователем;
        # слова «search» / «搜索» в них не доказывают назначение поля.
        values = [node.get(k) or "" for k in ("resource-id", "hint", "hint-text")]
        search_field = any("search" in value.lower() or "搜索" in value for value in values)
        nearby_button = False
        for other in root.iter("node"):
            if other is node or other.get("package") != PH.get("package"):
                continue
            labels = ((other.get("text") or "").strip().casefold(),
                      (other.get("content-desc") or "").strip().casefold())
            if not any(label in {"搜索", "search"} for label in labels):
                continue
            ob = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", other.get("bounds") or "")
            if not ob:
                continue
            ox1, oy1, ox2, oy2 = map(int, ob.groups())
            if (0 <= ox1 < ox2 <= sc["real_w"] and 0 <= oy1 < oy2 <= sc["real_h"]
                    and min(y2, oy2) > max(y1, oy1)
                    and max(0, x1 - ox2, ox1 - x2) <= sc["real_w"] * .2):
                nearby_button = True
                break
        if not search_field and not nearby_button:
            raise AdbError("Контекст поиска не подтверждён: Enter может отправить сообщение или выполнить другое действие.")
    identity = (class_name, node.get("resource-id") or "", x1, x2)
    if expected is not None and identity != expected:
        raise AdbError("Сфокусированное поле изменилось после ввода; Enter отменён.")
    _require_pdd_focus()
    return identity, node.get("text")


def _require_focused_editable(expected=None, require_search=False):
    identity, _ = _inspect_focused_editable(expected, require_search)
    return identity


def cmd_text(a):
    ensure_device()
    text = Path(a.file).read_text(encoding="utf-8").strip() if a.file else a.text
    if text is None:
        raise AdbError("Нужен текст или --file")
    with device_lock(SERIAL):
        _require_fresh_geometry()
        target = _require_focused_editable(require_search=a.enter)
        method = "input"
        warning = None
        kb = adbkeyboard_status()
        if kb["installed"]:
            if kb["authenticity"] == "tampered":
                raise AdbError(
                    "Установленный ADBKeyboard не совпал с закреплённым APK; ввод через него отменён. "
                    "Удалите пакет и установите заново из проверенного APK.")
            if kb["authenticity"] != "authentic":
                warning = (
                    "Подлинность установленного ADBKeyboard не подтверждена: пакет найден, "
                    "но его APK не удалось сверить с закреплённым SHA-256. Ввод продолжен только "
                    "с этим предупреждением; broadcast ограничен именем пакета.")
            prev = run_adb(["shell", "settings", "get", "secure", "default_input_method"], check=False).strip()
            run_adb(["shell", "ime", "enable", ADBKEYBOARD_IME], check=False)
            try:
                run_adb(["shell", "ime", "set", ADBKEYBOARD_IME], check=False)
                time.sleep(0.6)
                _require_focused_editable(target, require_search=a.enter)
                if a.clear:
                    run_adb(["shell", "am", "broadcast", "-p", ADBKEYBOARD_PACKAGE,
                             "-a", "ADB_CLEAR_TEXT"], check=False)
                    time.sleep(0.3)
                    _, cleared = _inspect_focused_editable(target, require_search=a.enter)
                    if cleared is None:
                        raise AdbError(
                            "Очистка поля через ADBKeyboard не подтверждена: атрибут text отсутствует; "
                            "текст не отправлен, ввод отменён.")
                    if cleared:
                        raise AdbError(
                            "Очистка поля через ADBKeyboard не подтверждена: поле не пусто; "
                            "текст не отправлен, ввод отменён.")
                b64 = base64.b64encode(text.encode("utf-8")).decode()
                _send_action_adb(["shell", "am", "broadcast", "-p", ADBKEYBOARD_PACKAGE,
                                  "-a", "ADB_INPUT_B64", "--es", "msg", b64],
                                 sensitive=True, redact=(text, b64))
                time.sleep(0.5)
                if a.enter:
                    _require_focused_editable(target, require_search=True)
                    _send_action_adb(["shell", "input", "keyevent", "66"])
            finally:
                if prev and "adbkeyboard" not in prev and prev != "null":
                    time.sleep(0.3)
                    run_adb(["shell", "ime", "set", prev], check=False)
            method = "adbkeyboard"
        else:
            if a.clear:
                raise AdbError(
                    "--clear не поддерживается без ADBKeyboard: надёжной очистки через `input` нет. "
                    "Текст не отправлен; установите ADBKeyboard или очистите поле вручную.")
            if not text.isascii():
                raise AdbError("Для ввода китайского/русского текста нужен ADBKeyboard: python pdd.py install-adbkeyboard")
            _require_focused_editable(target, require_search=a.enter)
            q = "'" + text.replace("'", "'\\''").replace(" ", "%s") + "'"
            _send_action_adb(["shell", "input", "text", q], sensitive=True, redact=(text, q))
            if a.enter:
                _require_focused_editable(target, require_search=True)
                _send_action_adb(["shell", "input", "keyevent", "66"])
        pace()
        result = {"ok": True, "action": "text", "method": method, "chars": len(text), "enter": a.enter,
                  "confirmation": "command_sent",
                  "adbkeyboard_authenticity": kb["authenticity"],
                  "note": "Ввод отправлен; изменение состояния не подтверждено. Проверьте свежий screen/ui."}
        if warning:
            result["warning"] = warning
        out(result)


KEYS = {"back": 4, "home": 3, "recent": 187, "del": 67, "search": 84}


def cmd_key(a):
    """Навигационные клавиши с проверкой окна и свежего дерева.

    Все клавиши требуют свежий полный дамп, чтобы действие не выполнялось на
    устаревшем/неполном экране. del/search дополнительно требуют
    подтверждённое поле PDD (для search — контекст поиска). Проверки не
    устраняют TOCTOU между последней проверкой и keyevent.
    """
    ensure_device()
    if a.key not in KEYS:
        raise AdbError("Клавиша запрещена: произвольный keyevent может активировать опасную кнопку.")
    with device_lock(SERIAL):
        _require_fresh_geometry()
        _require_pdd_focus()
        if a.key in ("del", "search"):
            # _require_focused_editable уже получает свежее несжатое дерево.
            _require_focused_editable(require_search=(a.key == "search"))
        else:
            read_ui_elements(include_all=True, require_full=True)
        _require_pdd_focus()
        code = KEYS[a.key]
        _send_action_adb(["shell", "input", "keyevent", code])
        pace()
        out({"ok": True, "action": "key", "key": a.key, "confirmation": "command_sent",
             "note": "Клавиша отправлена; изменение состояния не подтверждено. Проверьте свежий screen/ui."})


def cmd_launch(a):
    ensure_device()
    with device_lock(SERIAL):
        _send_action_adb(["shell", "monkey", "-p", PH["package"], "-c", "android.intent.category.LAUNCHER", "1"])
        time.sleep(3)
        out({"ok": True, "action": "launch", "package": PH["package"], "confirmation": "command_sent",
             "note": "Запуск отправлен; фактическое состояние не проверялось. Проверьте свежий screen/ui."})


def cmd_wait(a):
    time.sleep(a.seconds)
    out({"ok": True, "action": "wait", "seconds": a.seconds})


def cmd_current(a):
    ensure_device()
    txt = run_adb(["shell", "dumpsys", "window"], check=False)
    m = re.search(r"mCurrentFocus=.*?\s([\w.]+)/([\w.$]+)", txt)
    out({"package": m.group(1) if m else None, "activity": m.group(2) if m else None})


# ----------------------------------------------------------------- Setup
def cmd_connect(a):
    target = a.target or PH.get("serial")
    if not target:
        raise AdbError("Укажите IP:PORT")
    res = run_adb(["connect", target], use_serial=False, check=False, timeout=20,
                  sensitive=True, redact=(target,)).strip()
    if "connected" in res and "cannot" not in res and "failed" not in res:
        def change(cfg):
            cfg.setdefault("phone", {})["serial"] = target
            return cfg
        updated = update_config(change)
        CFG["phone"]["serial"] = updated["phone"]["serial"]
    out({"result": res, "saved_serial": CFG["phone"].get("serial")})


def cmd_pair(a):
    res = run_adb(["pair", a.target, a.code], use_serial=False, check=False, timeout=30,
                  sensitive=True, redact=(a.code,)).strip()
    if a.code:
        res = res.replace(a.code, "***")
    out({"result": res, "next": "Теперь: python pdd.py connect IP:PORT (порт подключения, не порт сопряжения)"})


def cmd_status(a):
    info = {"devices": list_devices(), "config_serial": PH.get("serial")}
    try:
        info["serial"] = ensure_device()
        info["model"] = run_adb(["shell", "getprop", "ro.product.model"], check=False).strip()
        info["android"] = run_adb(["shell", "getprop", "ro.build.version.release"], check=False).strip()
        info["screen"] = screen_info(refresh=True)
        info["pinduoduo_installed"] = PH["package"] in run_adb(["shell", "pm", "list", "packages", PH["package"]], check=False)
        keyboard = adbkeyboard_status()
        info["adbkeyboard_installed"] = keyboard["installed"]
        info["adbkeyboard"] = keyboard
        info["ok"] = True
    except (AdbError, StateError) as e:
        info["ok"] = False
        info["error"] = str(e)
    out(info)


def cmd_install_adbkeyboard(a):
    ensure_device()
    keyboard = adbkeyboard_status()
    if keyboard["installed"]:
        if keyboard["authenticity"] == "tampered":
            raise AdbError(
                "Установлен ADBKeyboard, но его APK не совпал с закреплённым SHA-256. "
                "Установка не выполняется; удалите пакет вручную и повторите.")
        if keyboard["authenticity"] == "authentic":
            out({"ok": True, "note": "ADBKeyboard уже установлен и совпал с закреплённым APK"})
        else:
            out({"ok": False, "installed": True, "authenticity": "unknown",
                 "note": "ADBKeyboard установлен, но подлинность не подтверждена: "
                         f"{keyboard.get('reason', 'причина неизвестна')}. Установка не выполнялась."})
        return
    apk = ROOT / "tools" / "ADBKeyboard.apk"
    apk.parent.mkdir(exist_ok=True)
    if not apk.exists():
        urllib.request.urlretrieve(ADBKEYBOARD_URL, apk)
    verify_adbkeyboard_apk(apk)
    with device_lock(SERIAL):
        res = run_adb(["install", "-r", str(apk)], check=False, timeout=120)
        run_adb(["shell", "ime", "enable", ADBKEYBOARD_IME], check=False)
        out({"ok": "Success" in res, "result": res.strip(), "confirmation": "command_sent",
             "hint": "Если установка отклонена: на Xiaomi включите «Установка через USB»; на телефоне подтвердите установку."})


# ----------------------------------------------------------------- Dossier
def cmd_dossier(a):
    paths = [Path(p) for p in a.images]
    for p in paths:
        if not p.exists():
            raise AdbError(f"Нет файла {p}")
    labels = (a.labels.split("|") if a.labels else [])
    labels += [f"{i + 1}" for i in range(len(labels), len(paths))]
    d = log_dir("dossiers")
    st = load_state()
    n = int(st.get("shot", 0))
    if not a.collage:
        out({"images": [str(p) for p in paths], "labels": labels,
             "hint": "Отправляйте как несколько вложений одного сообщения: chat.py send --image ... --image ..."})
        return
    imgs = [Image.open(p).convert("RGB") for p in paths]
    th = a.height
    imgs = [im.resize((round(im.width * th / im.height), th), Image.LANCZOS) for im in imgs]
    cols = min(a.cols, len(imgs))
    rows = math.ceil(len(imgs) / cols)
    pw = max(im.width for im in imgs)
    head = 40
    canvas = Image.new("RGB", (cols * pw + (cols - 1) * 8, rows * (th + head) + (rows - 1) * 8), "white")
    dr = ImageDraw.Draw(canvas)
    f = _font(26)
    for i, im in enumerate(imgs):
        r, c = divmod(i, cols)
        x, y = c * (pw + 8), r * (th + head + 8)
        dr.rectangle([x, y, x + pw, y + head], fill=(30, 30, 30))
        dr.text((x + 10, y + 6), labels[i], fill="white", font=f)
        canvas.paste(im, (x, y + head))
    p = d / f"d{n:04d}_collage.jpg"
    canvas.save(p, quality=85)
    out({"collage": str(p), "size": list(canvas.size)})


PHOTO_REMOTE_DIRS = ("/sdcard/DCIM/Camera", "/sdcard/Pictures")
PHOTO_NAME_PREFIX = "pdd_search_ref"


def _safe_photo_name(name):
    """Один компонент имени без разделителей и переходов по каталогам."""
    if not isinstance(name, str) or not name:
        raise AdbError("Некорректное имя файла для устройства")
    if "/" in name or "\\" in name or name in (".", "..") or "\x00" in name:
        raise AdbError("Некорректное имя файла для устройства: разделители пути и переходы запрещены")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", name):
        raise AdbError("Некорректное имя файла для устройства: допустимы только буквы, цифры, "
                       "точка, подчёркивание и дефис")
    return name


def _photo_remote_path(directory, name):
    if directory not in PHOTO_REMOTE_DIRS:
        raise AdbError("Недопустимая папка на устройстве для фото поиска")
    return f"{directory.rstrip('/')}/{_safe_photo_name(name)}"


def _photo_candidate_name():
    stamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    return f"{PHOTO_NAME_PREFIX}_{stamp}_{uuid.uuid4().hex[:10]}.jpg"


def _unique_local_photo_path():
    base = ROOT / "logs"
    base.mkdir(parents=True, exist_ok=True)
    for _ in range(5):
        candidate = base / _photo_candidate_name()
        if not candidate.exists():
            return candidate
    raise AdbError("Не удалось подобрать свободное локальное имя для фото")


def _remote_file_exists(path):
    res = run_adb(["shell", f"test -e {path} && echo EXISTS || echo MISSING"], check=False)
    text = (res or "").strip()
    if "EXISTS" in text:
        return True
    if "MISSING" in text:
        return False
    raise AdbError("Не удалось проверить существование файла на устройстве; загрузка отменена.")


def _push_unique_remote_photo(local_path, directory):
    """Проверяет отсутствие файла и загружает под уникальным именем.

    adb push не предоставляет атомарного create-only: между проверкой и
    отправкой остаётся гонка. Уникальное имя (время + случайный суффикс) и
    проверка существования снижают риск перезаписи, но не дают атомарной
    гарантии на устройстве.
    """
    for _ in range(5):
        candidate = _photo_remote_path(directory, _photo_candidate_name())
        if _remote_file_exists(candidate):
            continue
        _send_action_adb(["push", str(local_path), candidate])
        return candidate
    raise AdbError("Не удалось подобрать свободное имя файла на устройстве; загрузка отменена.")


def cmd_push_photo(a):
    ensure_device()
    src = Path(a.image)
    if not src.exists():
        raise AdbError(f"Нет файла {src}")
    im = Image.open(src).convert("RGB")
    if a.crop:
        box = [int(v) for v in a.crop.split(",")]
        if len(box) != 4:
            raise AdbError("--crop требует 4 числа: x1,y1,x2,y2")
        im = im.crop(box)
    out_local = _unique_local_photo_path()
    im.save(out_local, "JPEG", quality=92)
    created = []
    try:
        with device_lock(SERIAL):
            for directory in PHOTO_REMOTE_DIRS:
                remote = _push_unique_remote_photo(out_local, directory)
                created.append(remote)
                run_adb(["shell", "am", "broadcast", "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
                         "-d", f"file://{remote}"], check=False)
    except AdbError as exc:
        raise AdbError(
            f"Фото не загружено во все папки. Созданы только: {created or 'нет'}. Ошибка: {exc}") from exc
    out({"ok": True, "local_jpg": str(out_local), "size": list(im.size), "remote": created,
         "confirmation": "command_sent",
         "hint": "Файлы отправлены под уникальными именами; появление в галерее не подтверждено. "
                 "Проверьте экран. В Pinduoduo нажмите иконку фотоаппарата в строке поиска (y=115) "
                 "и выберите первое фото."})


# ----------------------------------------------------------------- CLI
def _stale_files(root: Path, cutoff: float):
    """Старые обычные файлы; символические ссылки пропускаются целиком.

    Ссылки не разыменовываются и не удаляются: цель ссылки вне разрешённой
    области не затрагивается. Символическая ссылка на сам корень тоже не
    обходится.
    """
    if not root.exists() or root.is_symlink():
        return []
    stale = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if not Path(dirpath, d).is_symlink()]
        for name in filenames:
            path = Path(dirpath, name)
            try:
                if path.is_symlink():
                    continue
                if path.is_file() and path.stat().st_mtime < cutoff:
                    stale.append(path)
            except OSError:
                continue
    return stale


def _log_root(name):
    """Разрешённая папка логов; символическая ссылка или выход наружу отсекаются."""
    base = ROOT / "logs"
    root = base / name
    try:
        if base.is_symlink() or root.is_symlink():
            return None
        resolved = root.resolve()
        if not resolved.is_relative_to(base.resolve()):
            return None
    except OSError:
        return None
    return root


def cmd_clean(a):
    if a.days < 1:
        raise AdbError("--days должен быть не меньше 1")
    cutoff = time.time() - a.days * 86400
    names = ("screens", "chats", "dossiers", "ui")
    found = []
    roots = []
    for name in names:
        root = _log_root(name)
        if root is None:
            continue
        roots.append(root)
        found += _stale_files(root, cutoff)
    total = 0
    for path in found:
        try:
            total += path.lstat().st_size
        except OSError:
            pass
    deleted = []
    failures = []
    if a.apply:
        for path in found:
            try:
                path.unlink()
                deleted.append(path)
            except OSError as exc:
                failures.append({"path": str(path), "error": exc.strerror or str(exc)})
        for root in roots:
            if not root.exists() or root.is_symlink():
                continue
            for folder in sorted(
                    (p for p in root.rglob("*") if p.is_dir() and not p.is_symlink()),
                    key=lambda p: len(p.parts), reverse=True):
                try:
                    folder.rmdir()
                except OSError:
                    pass
    if not a.apply:
        status = "dry_run"
    elif failures and deleted:
        status = "partial"
    elif failures:
        status = "failed"
    else:
        status = "ok"
    result = {"ok": status in ("dry_run", "ok"), "status": status, "days": a.days,
              "files": len(found), "found": len(found), "deleted": len(deleted),
              "failed": len(failures), "bytes": total, "applied": bool(a.apply),
              "errors": failures,
              "hint": "Без --apply только считает; с --apply удаляет файлы старше срока. "
                      "Профиль браузера, state.json и tools/ не затрагиваются. "
                      "Символические ссылки пропускаются: их цели не читаются и не удаляются."}
    if a.apply:
        result["deleted_paths"] = [str(p) for p in deleted]
    else:
        result["would_delete"] = [str(p) for p in found]
    out(result)


def main():
    ap = argparse.ArgumentParser(description="ADB-руки Оператора Pinduoduo")
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("status")
    p = sp.add_parser("connect"); p.add_argument("target", nargs="?")
    p = sp.add_parser("pair"); p.add_argument("target"); p.add_argument("code")
    sp.add_parser("install-adbkeyboard")
    p = sp.add_parser("screen"); p.add_argument("--grid", action="store_true", default=True); p.add_argument("--no-grid", dest="grid", action="store_false")
    p = sp.add_parser("ui"); p.add_argument("--find"); p.add_argument("--all", action="store_true"); p.add_argument("--no-log", action="store_true", help="Не записывать latest.txt; состояние для tapel сохраняется, временный дамп создаётся на телефоне")
    p = sp.add_parser("ui-diagnostic", help="Только просмотр экспериментального дерева без ожидания idle; не для нажатий")
    p.add_argument("--find"); p.add_argument("--all-packages", action="store_true")
    p = sp.add_parser("ui-short-idle", help="Только просмотр активного окна с коротким ожиданием; не для нажатий")
    p.add_argument("--find")
    p = sp.add_parser("tap"); p.add_argument("x", type=float); p.add_argument("y", type=float); p.add_argument("--long", type=int, default=0, help="Сохранён для совместимости, но заблокирован как жест до приёмки guard'а жестов"); p.add_argument("--force", action="store_true", help="Пропустить проверку неизвестных слоёв (жёсткие запреты сохраняются)")
    p = sp.add_parser("tapel"); p.add_argument("indices", type=int, nargs="+"); p.add_argument("--long", type=int, default=0, help="Сохранён для совместимости, но заблокирован как жест до приёмки guard'а жестов")
    p = sp.add_parser("scroll", help="Заблокировано до приёмки геометрии и guard'а жестов")
    p.add_argument("direction", choices=["down", "up", "left", "right"])
    p.add_argument("--amount", type=float, default=0.6); p.add_argument("--ms", type=int, default=450)
    p = sp.add_parser("swipe", help="Заблокировано до приёмки геометрии и guard'а жестов")
    for k in ("x1", "y1", "x2", "y2"):
        p.add_argument(k, type=float)
    p.add_argument("--ms", type=int, default=400)
    sp.add_parser("close-player", help="Явно закрыть подтверждённый мини-плеер; чтение само не нажимает")
    p = sp.add_parser("text"); p.add_argument("text", nargs="?"); p.add_argument("--file")
    p.add_argument("--enter", action="store_true"); p.add_argument("--clear", action="store_true")
    p = sp.add_parser("key"); p.add_argument("key")
    sp.add_parser("back"); sp.add_parser("home"); sp.add_parser("launch"); sp.add_parser("current")
    p = sp.add_parser("wait"); p.add_argument("seconds", type=float)
    p = sp.add_parser("push-photo"); p.add_argument("image"); p.add_argument("--crop", help="x1,y1,x2,y2")
    p = sp.add_parser("dossier"); p.add_argument("images", nargs="+"); p.add_argument("--labels")
    p.add_argument("--collage", action="store_true"); p.add_argument("--cols", type=int, default=2)
    p.add_argument("--height", type=int, default=1400)
    p = sp.add_parser("clean"); p.add_argument("--days", type=int, default=7); p.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    if a.cmd in ("back", "home"):
        a.key = a.cmd
        a.cmd = "key"
    fn = {"status": cmd_status, "connect": cmd_connect, "pair": cmd_pair, "install-adbkeyboard": cmd_install_adbkeyboard,
          "screen": cmd_screen, "ui": cmd_ui, "ui-diagnostic": cmd_ui_diagnostic, "ui-short-idle": cmd_ui_short_idle, "tap": cmd_tap, "tapel": cmd_tapel, "scroll": cmd_scroll,
          "swipe": cmd_swipe, "close-player": cmd_close_player, "text": cmd_text, "key": cmd_key, "launch": cmd_launch, "wait": cmd_wait,
          "current": cmd_current, "push-photo": cmd_push_photo, "dossier": cmd_dossier, "clean": cmd_clean}[a.cmd]
    try:
        fn(a)
    except (AdbError, StateError) as e:
        out({"ok": False, "error": str(e)})
        sys.exit(2)


if __name__ == "__main__":
    main()
