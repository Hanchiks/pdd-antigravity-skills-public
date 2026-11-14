"""Read-only experiment with the Android system dumper and short idle wait.

The resulting XML must never authorize input: Android may omit nodes and the
screen can change during capture. Both helper copies are checked and the
per-invocation remote JAR is removed even when execution fails.
"""
import hashlib
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

HELPER_SHA256 = "92871c989012fbbaa22f3fa0f2e44892c9f1fc17e1feefe6c59e258d12434d83"
MAX_XML_CHARS = 20_000_000


def read_short_idle(run_adb, helper_path, error_type):
    try:
        digest = hashlib.sha256(Path(helper_path).read_bytes()).hexdigest()
    except OSError as exc:
        raise error_type("Помощник короткого ожидания недоступен.") from exc
    if digest != HELPER_SHA256:
        raise error_type("Контрольная сумма помощника короткого ожидания не совпала; запуск отменён.")
    remote = "/data/local/tmp/pdd_ui_short_" + uuid.uuid4().hex + ".jar"
    try:
        run_adb(["push", str(helper_path), remote], timeout=15)
        checksum = run_adb(["shell", "sha256sum", remote], timeout=5).strip().split()
        if not checksum or checksum[0].lower() != HELPER_SHA256:
            raise error_type("Контрольная сумма помощника на телефоне не совпала; запуск отменён.")
        try:
            xml = run_adb(["exec-out", "env", "CLASSPATH=/system/framework/uiautomator.jar:" + remote,
                           "app_process", "/system/bin", "ShortIdleDump"], timeout=20)
        except error_type as exc:
            # ADB may include stdout in its error message; never echo a partial
            # hierarchy, which can contain private on-screen information.
            if "SHORT_IDLE_TIMEOUT" in str(exc):
                raise error_type("Экран не достиг краткого состояния покоя; просмотр отменён.") from exc
            raise error_type("Помощник короткого ожидания не вернул дерево; просмотр отменён.") from exc
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
