"""Conservative parser for one observed Android 16 input-dispatch dump format.

Used before the automatic player-close tap. It cannot detect views inside a
single app window or prevent a window from changing after the snapshot.
"""
import re


class UntrustedInputWindow(ValueError):
    """The snapshot does not prove the expected window receives the touch."""


_RECT = r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]"
_REGION = re.compile(rf"(?:{_RECT})(?:\|{_RECT})*")
_NUM = re.compile(r"^\s+(\d+): name=(.*)$")
_FIELDS = re.compile(
    r"^name=(?P<name>.*?), id=(?P<id>\d+), displayId=(?P<display>\d+), "
    r"inputConfig=(?P<config>.*?), miuiFlags=(?P<miui>.*?), alpha=(?P<alpha>[0-9.]+), "
    r"frame=(?P<frame>\[-?\d+,-?\d+\]\[-?\d+,-?\d+\]), globalScale=(?P<scale>[0-9.]+), "
    r"applicationInfo\.name=(?P<application>.*?), applicationInfo\.token=(?P<app_token>.*?), "
    r"touchableRegion=(?P<region><empty>|\[-?\d+,-?\d+\]\[-?\d+,-?\d+\](?:\|\[-?\d+,-?\d+\]\[-?\d+,-?\d+\])*)"
    r", ownerPid=(?P<pid>\d+), ownerUid=(?P<uid>\d+), dispatchingTimeout=\d+ms, "
    r"token=(?P<token>0x[0-9a-fA-F]+), touchOcclusionMode=(?P<occlusion>[A-Z_]+), "
    r"needMiuiEmbeddedEventMapping=0, miuiEmbeddedHotRegion=<empty>, "
    r"miuiEmbeddedMidRegion=<empty>(?:, canOccludePresentation)?$"
)
_KNOWN_FLAGS = {"NOT_FOCUSABLE", "NOT_TOUCHABLE", "NOT_VISIBLE", "NO_INPUT_CHANNEL",
                "TRUSTED_OVERLAY", "SPY", "WATCH_OUTSIDE_TOUCH", "SLIPPERY"}
_SYSTEM_GESTURES = {"[Gesture Monitor] WMShell", "[Gesture Monitor] miui-gesture"}


def _rectangles(value):
    if value == "<empty>":
        return []
    if not _REGION.fullmatch(value):
        raise UntrustedInputWindow("Некорректная область ввода.")
    found = [tuple(map(int, match)) for match in re.findall(_RECT, value)]
    if any(r <= l or b <= t for l, t, r, b in found):
        raise UntrustedInputWindow("Некорректные границы области ввода.")
    return found


def _contains(rectangles, x, y):
    return any(l <= x < r and t <= y < b for l, t, r, b in rectangles)


def require_expected_touch_window(dump, component, uid, x, y, window_hash):
    """Validate (only) observed 1080x2400, display-0, unrotated MIUI input dumps.

    component, uid and window_hash must be independently retrieved from Android
    for PDD's focused activity and exact package. Never use unchecked state.
    Raises on any unknown or ambiguous state; success is still a snapshot.
    """
    if not (isinstance(dump, str) and isinstance(component, str)
            and re.fullmatch(r"com\.xunmeng\.pinduoduo/[A-Za-z0-9_.$]+", component)
            and isinstance(uid, int) and not isinstance(uid, bool) and uid > 0
            and isinstance(window_hash, str) and re.fullmatch(r"[0-9a-fA-F]+", window_hash)
            and isinstance(x, int) and not isinstance(x, bool)
            and isinstance(y, int) and not isinstance(y, bool)
            and 0 <= x < 1080 and 132 <= y < 2100):
        raise UntrustedInputWindow("Не подтверждены параметры проверки окна.")
    dump = dump.replace("\r\n", "\n")
    markers = list(re.finditer(r"(?m)^[ \t]*Input Dispatcher State:[ \t]*\r?$", dump))
    if len(markers) != 1:
        raise UntrustedInputWindow("Не найден единственный текущий InputDispatcher.")
    live = dump[markers[0].end():].split("Input Dispatcher State at time of last ANR:", 1)[0]
    # Display names and indentation are intentionally checked, not guessed.
    displays = list(re.finditer(r"(?m)^  Display: (\d+)\s*$", live))
    selected = [m for m in displays if m.group(1) == "0"]
    if len(selected) != 1:
        raise UntrustedInputWindow("Нет единственного раздела дисплея 0.")
    begin = selected[0].end()
    end = next((m.start() for m in displays if m.start() > begin), len(live))
    display = live[begin:end]
    header = re.match(r"\s*logicalSize=1080x2400\s*\n\s*transform \(ROT_0\) \(IDENTITY\)\s*\n\s*Windows:\s*\n", display)
    if not header:
        raise UntrustedInputWindow("Формат дисплея или ориентация не подтверждены.")
    body = display[header.end():]
    next_section = re.search(r"(?m)^  \S", body)
    if not next_section:
        raise UntrustedInputWindow("Нет границы списка окон; снимок может быть неполным.")
    body = body[:next_section.start()]
    for line in body.splitlines():
        if (not line.strip() or re.fullmatch(r"      \d+: .+", line)
                or re.fullmatch(r"      transform \(ROT_\d+\) \([A-Z_| ]+\)", line)
                or re.fullmatch(r"          -?\d+\.\d+ + -?\d+\.\d+ + -?\d+\.\d+", line)):
            continue
        raise UntrustedInputWindow("Неизвестная строка в списке окон.")
    # Capture only window entry lines and their indented transform details.
    sections = list(re.finditer(r"(?m)^      (\d+): (.*)$", body))
    if not sections or [int(m.group(1)) for m in sections] != list(range(len(sections))):
        raise UntrustedInputWindow("Список окон неполон или неоднозначен.")
    if any(not _FIELDS.fullmatch(match.group(2)) for match in sections):
        raise UntrustedInputWindow("Неизвестный формат окна ввода.")
    for index, match in enumerate(sections):
        detail = _FIELDS.fullmatch(match.group(2))
        if not detail:
            raise UntrustedInputWindow("Неизвестный формат окна ввода.")
        f = detail.groupdict()
        if f["display"] != "0":
            raise UntrustedInputWindow("Окно относится к другому дисплею.")
        flags = set() if f["config"] == "0x0" else set(f["config"].split(" | "))
        if not flags <= _KNOWN_FLAGS or not re.fullmatch(r"0x[0-9a-fA-F]+|BOOST_RT", f["miui"]):
            raise UntrustedInputWindow("Неизвестные признаки окна ввода.")
        try:
            alpha, scale = float(f["alpha"]), float(f["scale"])
        except ValueError as exc:
            raise UntrustedInputWindow("Некорректные параметры окна.") from exc
        if not (0 <= alpha <= 1) or scale != 1:
            raise UntrustedInputWindow("Не подтверждён масштаб окна.")
        frame_values = tuple(map(int, re.fullmatch(_RECT, f["frame"]).groups()))
        left, top, right, bottom = frame_values
        if right < left or bottom < top:
            raise UntrustedInputWindow("Некорректные границы окна.")
        frame = [] if right == left or bottom == top else [frame_values]
        region = _rectangles(f["region"])
        if "NOT_VISIBLE" in flags:
            continue
        is_gesture = (f["name"] in _SYSTEM_GESTURES and int(f["uid"]) == 1000
                      and flags == {"NOT_FOCUSABLE", "TRUSTED_OVERLAY", "SPY"}
                      and not frame and f["miui"] == "0x0" and int(f["token"], 16) != 0)
        if is_gesture:
            continue
        if not (_contains(frame, x, y) or _contains(region, x, y)):
            continue
        # Ignore neither another app's translucent window nor an in-app popup.
        if (f["name"] != window_hash + " " + component
                or int(f["uid"]) != uid or flags
                or f["miui"] != "0x0" or alpha != 1 or int(f["token"], 16) == 0
                or not _contains(frame, x, y) or not _contains(region, x, y)):
            raise UntrustedInputWindow("В точке может находиться другое окно.")
        after = sections[index + 1].start() if index + 1 < len(sections) else len(body)
        transforms = body[match.end():after].strip().splitlines()
        if not transforms or transforms[0].strip() != "transform (ROT_0) (IDENTITY)":
            raise UntrustedInputWindow("Преобразование окна не подтверждено.")
        return
    raise UntrustedInputWindow("Окно Pinduoduo в точке нажатия не подтверждено.")
