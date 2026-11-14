"""Connect an Android device over Wi-Fi with a pairing code and explicit endpoints.

No QR generation, no background worker, no persisted secrets: one short
pair -> connect -> verify run.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

HIDDEN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
VERIFY_TOKEN = "codex-adb-pair-ok"


def emit(value):
    print(json.dumps(value, ensure_ascii=False))
    sys.stdout.flush()


def normalized_name(name):
    return name.rstrip(".").casefold().removesuffix(".local")


def endpoint_host(endpoint):
    host, separator, port = endpoint.rpartition(":")
    if not separator or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError("invalid_endpoint")
    host = host.strip("[]").strip()
    if not host:
        raise ValueError("invalid_endpoint")
    try:
        return str(ipaddress.ip_address(host)).casefold()
    except ValueError:
        return host.rstrip(".").casefold()


def valid_code(code):
    return bool(re.fullmatch(r"\d{6}", code))


def redact(text, secret):
    if secret:
        text = text.replace(secret, "***")
    return text


def one_line(text, limit=300):
    collapsed = " ".join((text or "").split())
    return collapsed[:limit]


@dataclass(frozen=True)
class Device:
    serial: str


def parse_devices(output):
    devices = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(Device(parts[0]))
    return devices


class Adb:
    def __init__(self, executable):
        self.executable = executable

    def run(self, *args, timeout=8):
        try:
            result = subprocess.run(
                [self.executable, *args], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout,
                creationflags=HIDDEN,
            )
            return result.returncode, result.stdout + "\n" + result.stderr
        except subprocess.TimeoutExpired:
            return 124, "command_timeout"
        except OSError:
            return 127, "adb_unavailable"

    def verify(self, serial):
        code, output = self.run("-s", serial, "shell", "echo", VERIFY_TOKEN, timeout=5)
        return code == 0 and VERIFY_TOKEN in output.splitlines()


def resolve_adb(explicit=None):
    if explicit:
        found = Path(explicit).expanduser().resolve()
        if found.is_file():
            return str(found)
        raise RuntimeError("adb_not_found")
    found = shutil.which("adb")
    if found:
        return found
    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
    candidates = [local / "Android/Sdk/platform-tools/adb.exe"]
    for variable in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(variable):
            candidates.append(Path(os.environ[variable]) / "platform-tools/adb.exe")
    winget = local / "Microsoft/WinGet/Packages"
    candidates += list(winget.glob("Genymobile.scrcpy_*/scrcpy*/adb.exe"))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    raise RuntimeError("adb_not_found")


def guid_matches(serial, guid):
    """Match an ADB serial to the pairing GUID without accepting a prefix-only twin."""
    guid = normalized_name(guid)
    if not guid:
        return False
    serial = normalized_name(serial)
    if serial == guid:
        return True
    return serial.startswith(guid + "-")


def candidate_serials(devices, connect_endpoint, guid):
    """Return only devices attributable to this pairing: GUID first, then exact endpoint."""
    candidates = []
    if guid:
        candidates = [d.serial for d in devices if guid_matches(d.serial, guid)]
    endpoint = connect_endpoint.strip().casefold()
    for device in devices:
        serial = device.serial.casefold()
        if (serial == endpoint or serial.startswith(endpoint)) and device.serial not in candidates:
            candidates.append(device.serial)
    return candidates


def pair_code(args):
    code = str(args.code).strip()
    if not valid_code(code):
        return {"state": "error", "error": "invalid_code",
                "hint": "Нужен ровно 6 цифр из диалога сопряжения.", "verified_now": False}

    pairing = args.pairing_endpoint.strip()
    connecting = args.connect_endpoint.strip()
    try:
        for endpoint in (pairing, connecting):
            endpoint_host(endpoint)
    except ValueError:
        return {"state": "error", "error": "invalid_endpoint",
                "hint": "Нужен формат IP:порт, например 192.168.1.50:40000.",
                "verified_now": False}

    adb = Adb(resolve_adb(args.adb))
    adb.run("start-server", timeout=5)

    pair_rc, pair_out = adb.run("pair", pairing, code, timeout=15)
    paired = pair_rc == 0 and "successfully paired" in pair_out.casefold()
    if not paired:
        return {"state": "error", "error": "pair_failed",
                "detail": one_line(redact(pair_out, code)),
                "hint": "Проверьте код и порт сопряжения; диалог на телефоне должен быть открыт.",
                "verified_now": False}

    match = re.search(r"\[guid=([^\]\s]+)\]", pair_out, re.I)
    guid = match.group(1) if match else None

    connect_rc, connect_out = adb.run("connect", connecting, timeout=10)
    connect_failed = connect_rc != 0 or "cannot" in connect_out.casefold() or "failed" in connect_out.casefold()

    _, devices_out = adb.run("devices", "-l", timeout=6)
    devices = parse_devices(devices_out)
    for serial in candidate_serials(devices, connecting, guid):
        if not adb.verify(serial):
            continue
        _, model_out = adb.run("-s", serial, "shell", "getprop", "ro.product.model", timeout=4)
        _, version_out = adb.run("-s", serial, "shell", "getprop", "ro.build.version.release", timeout=4)
        return {
            "state": "connected",
            "serial": serial,
            "model": one_line(model_out, 80),
            "android_version": one_line(version_out, 40),
            "paired": True,
            "verified_now": True,
        }

    return {
        "state": "error",
        "error": "connect_failed" if connect_failed else "device_not_verified",
        "paired": True,
        "detail": one_line(redact(connect_out, code)),
        "hint": ("Порт подключения берётся с главного экрана «Беспроводная отладка», "
                 "не из диалога сопряжения. Телефон и ПК должны быть в одной сети."),
        "verified_now": False,
    }


def build_parser():
    parser = argparse.ArgumentParser(
        description="Connect Android Wi-Fi debugging by pairing code and IP:port endpoints. No QR.")
    commands = parser.add_subparsers(dest="command", required=True)
    pair = commands.add_parser("pair-code", help="Pair with a 6-digit code, connect, and verify ADB shell")
    pair.add_argument("--code", required=True, help="6-digit pairing code from the phone dialog")
    pair.add_argument("--pairing-endpoint", required=True, help="IP:port shown in the pairing-code dialog")
    pair.add_argument("--connect-endpoint", required=True, help="IP:port shown on the main wireless-debugging screen")
    pair.add_argument("--adb", help="explicit path to adb.exe")
    return parser


def main():
    args = build_parser().parse_args()
    try:
        result = pair_code(args)
        emit(result)
        return 0 if result.get("state") == "connected" else 1
    except ValueError as error:
        emit({"state": "error", "error": str(error), "verified_now": False})
    except RuntimeError as error:
        emit({"state": "error", "error": str(error), "verified_now": False})
    except Exception as error:  # noqa: BLE001 - never leak arguments or secrets
        emit({"state": "error", "error": type(error).__name__, "verified_now": False})
    return 1


if __name__ == "__main__":
    sys.exit(main())