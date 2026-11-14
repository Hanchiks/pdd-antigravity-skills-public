---
name: adb-wifi-qr
description: >-
  Connect an Android phone over Wi-Fi using the 6-digit wireless-debugging pairing code and explicit pairing/connect IP:port endpoints, then verify ADB shell access. Use when the user supplies a code and ports or asks for wireless ADB. No QR codes, no background worker, no saved sessions.
---

# Android Wi-Fi Pairing By Code

Run on an explicit `$adb-wifi-qr` invocation, or when the user provides a 6-digit pairing code and endpoints. The folder name is historical: this skill no longer creates QR codes or keeps pairing sessions.

## Command

```powershell
python "SKILL_DIR\scripts\adb_wifi_qr.py" pair-code --code "CODE" --pairing-endpoint "IP:PORT" --connect-endpoint "IP:PORT"
```

Where the endpoints come from:

- `--pairing-endpoint`: the «Подключение с помощью кода сопряжения» dialog on the phone.
- `--connect-endpoint`: the main «Беспроводная отладка» screen.
- They normally share the host but use different ports. The pairing port is not the connect port.

Optional: `--adb "ABSOLUTE_PATH"` when `adb` is not on `PATH`.

## Output

The command prints one JSON object. Report connected only for `state=connected` and `verified_now=true`, including the serial, phone model, and Android version. For any other state, report the `error`, the short `hint`, and what to check; do not claim success from another attached phone.

## Recovery

- `adb_not_found`: locate an existing `adb.exe` (PATH, Android SDK platform-tools, scrcpy), then rerun with `--adb`. Ask for installation only if none exists.
- `invalid_code`: the code must be exactly 6 digits; reopen the pairing dialog.
- `pair_failed`: wrong or expired code, closed dialog, or the pairing port was used instead of the pairing endpoint. Do not retry the same code blindly.
- `connect_failed`: wrong connect port, phone and PC on different networks, or wireless debugging turned off.
- `device_not_verified`: pairing succeeded but shell access did not. Recheck the connect endpoint and retry once.
- `invalid_endpoint`: use `IP:PORT`, for example `192.168.1.50:40000`.

## Rules

- The code is passed as a command argument and is visible in the local process list; do not echo it into chat, logs, or reports.
- At most one retry after reopening the pairing dialog. Then stop and explain the actual state.
- Do not start QR pairing, background workers, or persistent sessions; do not modify firewall, mDNS, or ADB discovery settings automatically.
- Do not accept a device merely because `adb devices` lists it; only the GUID or exact endpoint of this pairing counts.