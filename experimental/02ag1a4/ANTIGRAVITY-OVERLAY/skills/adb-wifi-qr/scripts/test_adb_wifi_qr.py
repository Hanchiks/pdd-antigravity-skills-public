"""Offline behavioral checks for the code-only Wi-Fi pairing helper."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock


SCRIPT = Path(__file__).with_name("adb_wifi_qr.py")
SPEC = importlib.util.spec_from_file_location("adb_wifi_qr_under_test", SCRIPT)
qr = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = qr
SPEC.loader.exec_module(qr)

GUID = "adb-test-phone"
TARGET = GUID + "-random"
OTHER = "adb-unrelated-phone-random"
ENDPOINT = "192.168.1.50:40000"
PAIR_ENDPOINT = "192.168.1.50:45000"


def args(code="123456", pairing=PAIR_ENDPOINT, connecting=ENDPOINT, adb="mock-adb"):
    return SimpleNamespace(code=code, pairing_endpoint=pairing, connect_endpoint=connecting, adb=adb)


class FakeAdb:
    def __init__(self, devices=(), verified=(), pair=(0, f"Successfully paired [guid={GUID}]"),
                 connect=(0, f"connected to {ENDPOINT}"), failure=None):
        self.calls = []
        self.verifications = []
        self.devices = list(devices)
        self.verified = set(verified)
        self.pair = pair
        self.connect = connect
        self.failure = failure

    def run(self, *call, timeout=None):
        self.calls.append(call)
        if self.failure:
            raise self.failure
        if call[0] == "start-server":
            return 0, ""
        if call[0] == "pair":
            return self.pair
        if call[0] == "connect":
            return self.connect
        if call[:2] == ("devices", "-l"):
            lines = "List of devices attached\n" + "".join(f"{d}\tdevice\n" for d in self.devices)
            return 0, lines
        if call[0] == "-s" and call[2:4] == ("shell", "getprop"):
            return (0, "TestPhone\n") if call[4] == "ro.product.model" else (0, "14\n")
        return 0, ""

    def verify(self, serial):
        self.verifications.append(serial)
        return serial in self.verified


class HelperTests(unittest.TestCase):
    def test_code_must_be_six_digits(self):
        self.assertTrue(qr.valid_code("123456"))
        for code in ("12345", "1234567", "12a456", " 123456"):
            self.assertFalse(qr.valid_code(code))

    def test_endpoints_accept_ipv4_bracketed_ipv6_and_hostnames(self):
        self.assertEqual(qr.endpoint_host("192.168.1.5:37123"), "192.168.1.5")
        self.assertEqual(qr.endpoint_host("[fe80::1%7]:37123"), "fe80::1%7")
        self.assertEqual(qr.endpoint_host("phone.local:5555"), "phone.local")
        for bad in ("192.168.1.5", "192.168.1.5:0", "192.168.1.5:no", ":5555"):
            with self.assertRaises(ValueError):
                qr.endpoint_host(bad)

    def test_guid_match_requires_boundary(self):
        self.assertTrue(qr.guid_matches(TARGET.upper() + ".LOCAL.", GUID))
        self.assertFalse(qr.guid_matches(GUID + "X-random", GUID))
        self.assertFalse(qr.guid_matches("adb-other-random", GUID))

    def test_candidates_never_include_unrelated_devices(self):
        devices = [qr.Device(OTHER), qr.Device(TARGET)]
        self.assertEqual(qr.candidate_serials(devices, ENDPOINT, GUID), [TARGET])
        self.assertEqual(qr.candidate_serials(devices, ENDPOINT, None), [])
        self.assertEqual(qr.candidate_serials([qr.Device(ENDPOINT)], ENDPOINT, None), [ENDPOINT])


class PairCodeTests(unittest.TestCase):
    def run_pair(self, fake, argument=None):
        with mock.patch.object(qr, "resolve_adb", return_value="mock-adb"), \
                mock.patch.object(qr, "Adb", return_value=fake):
            return qr.pair_code(argument or args())

    def test_success_verifies_only_the_paired_device(self):
        fake = FakeAdb(devices=[OTHER, TARGET], verified=[OTHER, TARGET])
        result = self.run_pair(fake)
        self.assertEqual(result["state"], "connected")
        self.assertEqual(result["serial"], TARGET)
        self.assertEqual(result["model"], "TestPhone")
        self.assertEqual(result["android_version"], "14")
        self.assertTrue(result["verified_now"])
        self.assertEqual(fake.verifications, [TARGET])

    def test_unrelated_attached_phone_is_not_claimed(self):
        fake = FakeAdb(devices=[OTHER], verified=[OTHER])
        result = self.run_pair(fake)
        self.assertEqual(result["state"], "error")
        self.assertEqual(result["error"], "device_not_verified")
        self.assertEqual(fake.verifications, [])

    def test_falls_back_to_exact_connect_endpoint_when_guid_is_unknown(self):
        fake = FakeAdb(devices=[OTHER, ENDPOINT], verified=[ENDPOINT],
                       pair=(0, "Successfully paired"))
        result = self.run_pair(fake)
        self.assertEqual(result["state"], "connected")
        self.assertEqual(result["serial"], ENDPOINT)

    def test_pair_exit_code_must_be_zero_even_with_success_text(self):
        fake = FakeAdb(devices=[TARGET], verified=[TARGET],
                       pair=(1, f"Successfully paired [guid={GUID}]"))
        result = self.run_pair(fake)
        self.assertEqual(result["error"], "pair_failed")
        self.assertFalse(any(call[0] == "connect" for call in fake.calls))

    def test_pair_failure_does_not_echo_the_code(self):
        fake = FakeAdb(pair=(1, "Failed to authenticate 123456 at 192.168.1.50:45000"))
        result = self.run_pair(fake)
        self.assertEqual(result["error"], "pair_failed")
        self.assertNotIn("123456", result["detail"])

    def test_invalid_code_stops_before_adb(self):
        fake = FakeAdb()
        result = self.run_pair(fake, args(code="12ab"))
        self.assertEqual(result["error"], "invalid_code")
        self.assertEqual(fake.calls, [])

    def test_invalid_endpoint_is_reported(self):
        fake = FakeAdb()
        result = self.run_pair(fake, args(connecting="192.168.1.50"))
        self.assertEqual(result["error"], "invalid_endpoint")
        self.assertEqual(fake.calls, [])

    def test_device_that_fails_shell_check_is_not_connected(self):
        fake = FakeAdb(devices=[TARGET], verified=[])
        result = self.run_pair(fake)
        self.assertEqual(result["error"], "device_not_verified")
        self.assertFalse(result["verified_now"])


if __name__ == "__main__":
    unittest.main()