"""Read-only, experimental Android accessibility snapshot without idle wait.

The result is deliberately not accepted by the tap/tapel safety checks. Android
can omit nodes or change the hierarchy while it is being traversed.
"""
import hashlib
import re
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

HELPER_SHA256 = "42dab40233692b94ca4950db5d2f477880719526b8c17ac36a83d77725dbc357"
MAX_XML_CHARS = 20_000_000


def read_no_idle(run_adb, helper_path, error_type):
    """Push a verified helper, run it once, and remove only this invocation's file."""
    try:
        digest = hashlib.sha256(Path(helper_path).read_bytes()).hexdigest()
    except OSError as exc:
        raise error_type("Диагностический помощник недоступен.") from exc
    if digest != HELPER_SHA256:
        raise error_type("Контрольная сумма диагностического помощника не совпала; запуск отменён.")
    remote = "/data/local/tmp/pdd_ui_diag_" + uuid.uuid4().hex + ".jar"
    try:
        run_adb(["push", str(helper_path), remote], timeout=15)
        checksum = run_adb(["shell", "sha256sum", remote], timeout=5).strip().split()
        if not checksum or checksum[0].lower() != HELPER_SHA256:
            raise error_type("Контрольная сумма помощника на телефоне не совпала; запуск отменён.")
        xml = run_adb(["exec-out", "env", "CLASSPATH=/system/framework/uiautomator.jar:" + remote,
                       "app_process", "/system/bin", "NoIdleDump"], timeout=20)
        if len(xml) > MAX_XML_CHARS:
            raise error_type("Диагностическое дерево слишком велико; просмотр отменён.")
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as exc:
            raise error_type("Диагностическое дерево некорректно; просмотр отменён.") from exc
        if root.tag != "hierarchy" or next(root.iter("node"), None) is None:
            raise error_type("Диагностическое дерево пусто; просмотр отменён.")
        return root
    finally:
        try:
            run_adb(["shell", "rm", "-f", remote], check=False, timeout=5)
        except error_type:
            pass


def format_nodes(root, package, find=None, all_packages=False):
    """Format only visible values; output is never saved for a later tap."""
    lines = []
    packages = {n.get("package") for n in root.iter("node")}
    if package not in packages:
        raise ValueError("Pinduoduo не обнаружен в диагностическом дереве.")
    for n in root.iter("node"):
        if not all_packages and n.get("package") != package:
            continue
        text = (n.get("text") or "").strip()
        desc = (n.get("content-desc") or "").strip()
        rid = n.get("resource-id") or ""
        if not (text or desc or rid):
            continue
        if find and not any(find in value for value in (text, desc, rid)):
            continue
        def clean(value):
            return re.sub(r"[\x00-\x1f\x7f]", " ", value)[:200]
        fields = ["label=" + clean(text or desc), "id=" + clean(rid),
                  "class=" + clean(n.get("class") or ""), "bounds=" + clean(n.get("bounds") or ""),
                  "package=" + clean(n.get("package") or "")]
        lines.append(" | ".join(fields))
    return lines
