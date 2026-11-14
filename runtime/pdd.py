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

from common import ROOT, StateError, load_config, load_state, log_dir, out, save_config, update_state

CFG = load_config()
PH = CFG["phone"]
SERIAL = None
ADBKEYBOARD_IME = "com.android.adbkeyboard/.AdbIME"
ADBKEYBOARD_URL = "https://github.com/senzhk/ADBKeyBoard/raw/master/ADBKeyboard.apk"
ADBKEYBOARD_SHA256 = "e698adea5633135a067b038f9a0cf41baa4de09888713a81593fb2b9682cdc59"


class AdbError(RuntimeError):
    pass


class UiIdleError(AdbError):
    """Штатный дамп не получен из-за подтверждённой ошибки idle/таймаута."""


# ----------------------------------------------------------------- ADB basics
def run_adb(args, use_serial=True, binary=False, timeout=40, check=True, include_stderr=False):
    cmd = [PH.get("adb_path") or "adb"]
    if use_serial and SERIAL:
        cmd += ["-s", SERIAL]
    cmd += [str(a) for a in args]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise AdbError(f"Таймаут команды: {' '.join(cmd)}")
    if check and r.returncode != 0:
        msg = (r.stderr or r.stdout).decode("utf-8", errors="replace").strip()
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


def screen_info(refresh=False):
    st = load_state()
    sc = st.get("screen") or {}
    if sc.get("serial") == SERIAL and sc.get("real_w") and not refresh:
        return sc
    txt = run_adb(["shell", "wm", "size"])
    m = re.search(r"Override size:\s*(\d+)x(\d+)", txt) or re.search(r"Physical size:\s*(\d+)x(\d+)", txt)
    if not m:
        raise AdbError(f"Не удалось определить размер экрана: {txt}")
    rw, rh = int(m.group(1)), int(m.group(2))
    vw = int(PH.get("view_width", 720))
    scale = rw / vw
    sc = {"serial": SERIAL, "real_w": rw, "real_h": rh, "view_w": vw, "view_h": round(rh / scale), "scale": scale}
    update_state(lambda state: state.update({"screen": sc}))
    return sc


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


def capture():
    png = run_adb(["exec-out", "screencap", "-p"], binary=True, timeout=30)
    if not png.startswith(b"\x89PNG"):
        raise AdbError("screencap вернул не PNG (экран заблокирован или защищён от записи?)")
    return Image.open(io.BytesIO(png)).convert("RGB")


def cmd_screen(a):
    ensure_device()
    img = capture()
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


def _close_live_player_after_failed_dump():
    """Один ограниченный тап только по подтверждённому визуально мини-плееру."""
    try:
        _require_pdd_focus()
        first = capture()
        if not _detect_live_player(first):
            return False
        time.sleep(0.5)
        _require_pdd_focus()
        second = capture()
        if not _detect_live_player(second):
            return False
        # Видео должно действительно меняться: статичная картинка с X и
        # похожей плашкой не даёт разрешения на нажатие.
        region = (831, 585, 990, 855)
        a = first.crop(region).resize((32, 54)).convert("RGB").tobytes()
        b = second.crop(region).resize((32, 54)).convert("RGB").tobytes()
        if sum(max(abs(a[i + j] - b[i + j]) for j in range(3)) >= 30
               for i in range(0, len(a), 3)) < 40:
            return False
        _require_pdd_focus()
        _require_player_touch_window(1022, 542)
        tap_real(1022, 542)  # (681, 361) view, только проверенный крестик
        return True
    except (AdbError, OSError, TypeError, ValueError):
        return False


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
    xml, last, idle_like = _dump_ui_attempts((False,))
    if xml is not None:
        return xml
    attempted_close = idle_like
    player_closed = attempted_close and _close_live_player_after_failed_dump()
    if player_closed:
        # Повтор не может использовать старый XML и не закрывает плеер повторно.
        xml, last, retry_idle = _dump_ui_attempts((False,))
        if xml is not None:
            return xml
        idle_like = idle_like and retry_idle
    # Сжатый дамп предназначен для просмотра; read_ui_elements(require_full=True)
    # по-прежнему не допускает нажатия на его основании.
    xml, last, compressed_idle = _dump_ui_attempts((True,))
    if xml is not None:
        return xml
    idle_like = idle_like and compressed_idle
    if player_closed:
        player_detail = "Выполнена попытка закрыть распознанный мини-плеер, но дерево по-прежнему недоступно. "
    elif attempted_close:
        player_detail = "Мини-плеер не был автоматически закрыт: его наличие или безопасность не подтверждены. "
    else:
        player_detail = ""
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
def tap_real(rx, ry, long_ms=0):
    if long_ms:
        run_adb(["shell", "input", "swipe", rx, ry, rx, ry, long_ms])
    else:
        run_adb(["shell", "input", "tap", rx, ry])
    pace()


FORBIDDEN_LABELS = {"全选", "去结算", "立即支付", "提交订单", "确认支付"}
# Observed on the card next to another buyer's group; unlike 发起拼单 it
# enters an existing group and is not authorized by merely inspecting SKU.
GROUP_JOIN_LABELS = {"直接拼成"}


def _is_forbidden(e):
    return e.get("forbidden", False) or e.get("group_join_forbidden", False) or any(label in (e.get(k) or "")
               for k in ("label", "desc") for label in FORBIDDEN_LABELS | GROUP_JOIN_LABELS)


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


def cmd_tap(a):
    ensure_device()
    _require_pdd_focus()
    els = read_ui_elements(include_all=True, require_full=True)
    _check_target(a.x, a.y, els, force=getattr(a, "force", False))
    _require_pdd_focus()
    rx, ry = to_real(a.x, a.y)
    tap_real(rx, ry, a.long)
    out({"ok": True, "action": "tap", "view": [a.x, a.y], "real": [rx, ry]})


def cmd_tapel(a):
    ensure_device()
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
    tapped = []
    for idx in indices:
        e = els[idx]
        try:
            _require_pdd_focus()
            _require_fresh_ui(st)
            current = read_ui_elements(include_all=True, require_full=True)
            _require_fresh_ui(st)
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
        out({"ok": True, "action": "tapel", "label": tapped[0]["label"], "view": tapped[0]["view"]})
    else:
        out({"ok": True, "action": "tapel", "tapped": tapped, "label": tapped[-1]["label"], "view": tapped[-1]["view"]})


def cmd_scroll(a):
    ensure_device()
    _require_pdd_focus()
    if not math.isfinite(a.amount) or not 1 <= a.ms <= 2000:
        raise AdbError("Некорректная скорость или длительность прокрутки.")
    sc = screen_info()
    w, h = sc["real_w"], sc["real_h"]
    k = max(0.1, min(0.85, a.amount))
    cx, cy = w // 2, int(h * 0.55)
    dy, dx = int(h * k / 2), int(w * k / 2)
    # direction = куда сдвигается содержимое для просмотра (down = увидеть то, что ниже)
    vec = {"down": (cx, cy + dy, cx, cy - dy), "up": (cx, cy - dy, cx, cy + dy),
           "right": (cx + dx, cy, cx - dx, cy), "left": (cx - dx, cy, cx + dx, cy)}[a.direction]
    distance = math.hypot(vec[2] - vec[0], vec[3] - vec[1]) / sc["scale"]
    if distance < 80 or distance / a.ms < 0.12:
        raise AdbError("Слишком короткая или медленная прокрутка может сработать как нажатие; действие отменено.")
    run_adb(["shell", "input", "swipe", *vec, a.ms])
    pace()
    player_tapped = _close_live_player_after_failed_dump()
    out({"ok": True, "action": "scroll", "direction": a.direction, "amount": k,
         "player_close_attempted": player_tapped})


def cmd_swipe(a):
    ensure_device()
    _require_pdd_focus()
    sc = screen_info()
    coords = (a.x1, a.y1, a.x2, a.y2)
    if (not all(math.isfinite(v) for v in coords)
            or not (0 <= a.x1 < sc["view_w"] and 0 <= a.x2 < sc["view_w"]
                    and 88 <= a.y1 < sc["view_h"] and 88 <= a.y2 < sc["view_h"])):
        raise AdbError("Свайп выходит за пределы экрана или попадает в статус-бар.")
    distance = math.hypot(a.x2 - a.x1, a.y2 - a.y1)
    if not 1 <= a.ms <= 2000 or distance < 80 or distance / a.ms < 0.12:
        raise AdbError("Слишком короткий или медленный свайп может сработать как нажатие; действие отменено.")
    x1, y1 = to_real(a.x1, a.y1)
    x2, y2 = to_real(a.x2, a.y2)
    run_adb(["shell", "input", "swipe", x1, y1, x2, y2, a.ms])
    pace()
    player_tapped = _close_live_player_after_failed_dump()
    out({"ok": True, "action": "swipe", "player_close_attempted": player_tapped})


def has_adbkeyboard():
    return "com.android.adbkeyboard" in run_adb(["shell", "pm", "list", "packages", "com.android.adbkeyboard"], check=False)


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


def _require_focused_editable(expected=None, require_search=False):
    """Сверяет вводимый элемент по свежему несжатому дереву, без записи XML."""
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
    return identity


def cmd_text(a):
    ensure_device()
    text = Path(a.file).read_text(encoding="utf-8").strip() if a.file else a.text
    if text is None:
        raise AdbError("Нужен текст или --file")
    target = _require_focused_editable(require_search=a.enter)
    method = "input"
    if has_adbkeyboard():
        prev = run_adb(["shell", "settings", "get", "secure", "default_input_method"], check=False).strip()
        run_adb(["shell", "ime", "enable", ADBKEYBOARD_IME], check=False)
        try:
            run_adb(["shell", "ime", "set", ADBKEYBOARD_IME], check=False)
            time.sleep(0.6)
            _require_focused_editable(target, require_search=a.enter)
            if a.clear:
                run_adb(["shell", "am", "broadcast", "-a", "ADB_CLEAR_TEXT"], check=False)
            b64 = base64.b64encode(text.encode("utf-8")).decode()
            run_adb(["shell", "am", "broadcast", "-a", "ADB_INPUT_B64", "--es", "msg", b64])
            time.sleep(0.5)
            if a.enter:
                _require_focused_editable(target, require_search=True)
                run_adb(["shell", "input", "keyevent", "66"])
        finally:
            if prev and "adbkeyboard" not in prev and prev != "null":
                time.sleep(0.3)
                run_adb(["shell", "ime", "set", prev], check=False)
        method = "adbkeyboard"
    else:
        if not text.isascii():
            raise AdbError("Для ввода китайского/русского текста нужен ADBKeyboard: python pdd.py install-adbkeyboard")
        _require_focused_editable(target, require_search=a.enter)
        q = "'" + text.replace("'", "'\\''").replace(" ", "%s") + "'"
        run_adb(["shell", "input", "text", q])
        if a.enter:
            _require_focused_editable(target, require_search=True)
            run_adb(["shell", "input", "keyevent", "66"])
    pace()
    out({"ok": True, "action": "text", "method": method, "text": text, "enter": a.enter})


KEYS = {"back": 4, "home": 3, "recent": 187, "del": 67, "search": 84}


def cmd_key(a):
    ensure_device()
    if a.key not in KEYS:
        raise AdbError("Клавиша запрещена: произвольный keyevent может активировать опасную кнопку.")
    _require_pdd_focus()
    code = KEYS[a.key]
    run_adb(["shell", "input", "keyevent", code])
    pace()
    out({"ok": True, "action": "key", "key": a.key})


def cmd_launch(a):
    ensure_device()
    run_adb(["shell", "monkey", "-p", PH["package"], "-c", "android.intent.category.LAUNCHER", "1"])
    time.sleep(3)
    out({"ok": True, "action": "launch", "package": PH["package"]})


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
    res = run_adb(["connect", target], use_serial=False, check=False, timeout=20).strip()
    if "connected" in res and "cannot" not in res and "failed" not in res:
        CFG["phone"]["serial"] = target
        save_config(CFG)
    out({"result": res, "saved_serial": CFG["phone"].get("serial")})


def cmd_pair(a):
    res = run_adb(["pair", a.target, a.code], use_serial=False, check=False, timeout=30)
    out({"result": res.strip(), "next": "Теперь: python pdd.py connect IP:PORT (порт подключения, не порт сопряжения)"})


def cmd_status(a):
    info = {"devices": list_devices(), "config_serial": PH.get("serial")}
    try:
        info["serial"] = ensure_device()
        info["model"] = run_adb(["shell", "getprop", "ro.product.model"], check=False).strip()
        info["android"] = run_adb(["shell", "getprop", "ro.build.version.release"], check=False).strip()
        info["screen"] = screen_info(refresh=True)
        info["pinduoduo_installed"] = PH["package"] in run_adb(["shell", "pm", "list", "packages", PH["package"]], check=False)
        info["adbkeyboard_installed"] = has_adbkeyboard()
        info["ok"] = True
    except (AdbError, StateError) as e:
        info["ok"] = False
        info["error"] = str(e)
    out(info)


def cmd_install_adbkeyboard(a):
    ensure_device()
    if has_adbkeyboard():
        out({"ok": True, "note": "ADBKeyboard уже установлен"})
        return
    apk = ROOT / "tools" / "ADBKeyboard.apk"
    apk.parent.mkdir(exist_ok=True)
    if not apk.exists():
        urllib.request.urlretrieve(ADBKEYBOARD_URL, apk)
    verify_adbkeyboard_apk(apk)
    res = run_adb(["install", "-r", str(apk)], check=False, timeout=120)
    run_adb(["shell", "ime", "enable", ADBKEYBOARD_IME], check=False)
    out({"ok": "Success" in res, "result": res.strip(),
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
    out_local = ROOT / "logs" / "pdd_search_ref.jpg"
    out_local.parent.mkdir(parents=True, exist_ok=True)
    im.save(out_local, "JPEG", quality=92)
    remote_paths = ["/sdcard/DCIM/Camera/pdd_search_ref.jpg", "/sdcard/Pictures/pdd_search_ref.jpg"]
    for rp in remote_paths:
        run_adb(["push", str(out_local), rp])
        run_adb(["shell", "am", "broadcast", "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
                 "-d", f"file://{rp}"], check=False)
    out({"ok": True, "local_jpg": str(out_local), "size": list(im.size), "remote": remote_paths,
         "hint": "Фото добавлено в галерею (первое в списке). В Pinduoduo нажмите иконку фотоаппарата в строке поиска (y=115) и выберите первое фото."})


# ----------------------------------------------------------------- CLI
def _stale_files(root: Path, cutoff: float):
    if not root.exists():
        return []
    stale = []
    for path in root.rglob("*"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                stale.append(path)
        except OSError:
            continue
    return stale


def cmd_clean(a):
    if a.days < 1:
        raise AdbError("--days должен быть не меньше 1")
    cutoff = time.time() - a.days * 86400
    names = ("screens", "chats", "dossiers", "ui")
    targets = []
    for name in names:
        targets += _stale_files(ROOT / "logs" / name, cutoff)
    total = sum(path.stat().st_size for path in targets if path.exists())
    if a.apply:
        for path in targets:
            try:
                path.unlink()
            except OSError:
                pass
        for name in names:
            root = ROOT / "logs" / name
            if not root.exists():
                continue
            for folder in sorted((p for p in root.rglob("*") if p.is_dir()),
                                 key=lambda p: len(p.parts), reverse=True):
                try:
                    folder.rmdir()
                except OSError:
                    pass
    out({"ok": True, "days": a.days, "files": len(targets), "bytes": total,
         "applied": bool(a.apply),
         "hint": "Без --apply только считает; с --apply удаляет файлы старше срока. "
                 "Профиль браузера, state.json и tools/ не затрагиваются."})


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
    p = sp.add_parser("tap"); p.add_argument("x", type=float); p.add_argument("y", type=float); p.add_argument("--long", type=int, default=0); p.add_argument("--force", action="store_true", help="Пропустить проверку неизвестных слоёв (жёсткие запреты сохраняются)")
    p = sp.add_parser("tapel"); p.add_argument("indices", type=int, nargs="+"); p.add_argument("--long", type=int, default=0)
    p = sp.add_parser("scroll"); p.add_argument("direction", choices=["down", "up", "left", "right"])
    p.add_argument("--amount", type=float, default=0.6); p.add_argument("--ms", type=int, default=450)
    p = sp.add_parser("swipe")
    for k in ("x1", "y1", "x2", "y2"):
        p.add_argument(k, type=float)
    p.add_argument("--ms", type=int, default=400)
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
          "swipe": cmd_swipe, "text": cmd_text, "key": cmd_key, "launch": cmd_launch, "wait": cmd_wait,
          "current": cmd_current, "push-photo": cmd_push_photo, "dossier": cmd_dossier, "clean": cmd_clean}[a.cmd]
    try:
        fn(a)
    except (AdbError, StateError) as e:
        out({"ok": False, "error": str(e)})
        sys.exit(2)


if __name__ == "__main__":
    main()
