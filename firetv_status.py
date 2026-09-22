#!/usr/bin/env python3
"""Read foreground app and media session from a Fire OS TV over local ADB."""

import json
import ipaddress
import os
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor


APP_NAMES = {
    "com.amazon.firetv.youtube": "YouTube",
    "com.google.android.youtube.tv": "YouTube",
    "com.netflix.ninja": "Netflix",
    "com.amazon.avod.thirdpartyclient": "Prime Video",
    "com.amazon.firebat": "Prime Video",
    "com.spotify.tv.android": "Spotify",
    "com.disney.disneyplus": "Disney+",
    "com.hbo.hbonow": "Max",
    "com.plexapp.android": "Plex",
    "com.apple.atve.amazon.appletv": "Apple TV",
    "org.jellyfin.androidtv": "Jellyfin",
    "com.globo.globotv": "Globoplay",
    "tv.pluto.android": "Pluto TV",
    "com.amazon.tv.launcher": "Fire TV Home",
}

SHORTCUT_PACKAGES = tuple(package for package in APP_NAMES
                          if package != "com.amazon.tv.launcher")
APP_ICON_PATHS = {
    "com.amazon.firetv.youtube": "/usr/share/icons/Papirus/64x64/apps/youtube.svg",
    "com.google.android.youtube.tv": "/usr/share/icons/Papirus/64x64/apps/youtube.svg",
    "com.netflix.ninja": "/usr/share/icons/Papirus/64x64/apps/netflix.svg",
    "com.spotify.tv.android": "/usr/share/icons/hicolor/64x64/apps/spotify.png",
    "com.plexapp.android": "/usr/share/icons/Papirus/64x64/apps/plex-htpc.svg",
    "org.jellyfin.androidtv": "/usr/share/icons/Papirus/64x64/apps/jellyfin.svg",
}
HISTORY_PATH = Path.home() / ".local/state/omarchy/firetv/history.json"
HISTORY_BASELINE_PATH = HISTORY_PATH.with_name("history-baseline.txt")

ACTION_PLAY = 4
ACTION_PAUSE = 2
ACTION_PREVIOUS = 16
ACTION_NEXT = 32
ACTION_REWIND = 8
ACTION_FAST_FORWARD = 64

REMOTE_KEYS = {
    "up": "KEYCODE_DPAD_UP", "down": "KEYCODE_DPAD_DOWN",
    "left": "KEYCODE_DPAD_LEFT", "right": "KEYCODE_DPAD_RIGHT",
    "select": "KEYCODE_DPAD_CENTER", "back": "KEYCODE_BACK",
    "home": "KEYCODE_HOME",
}


def adb(serial, *args, timeout=8):
    try:
        result = subprocess.run(
            ["adb", "-s", serial, *args],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, str(error)
    if result.returncode:
        return None, (result.stderr or result.stdout).strip()
    return result.stdout, None


def valid_host(value):
    try:
        address = ipaddress.ip_address(value)
        return address.version == 4 and address.is_private and not address.is_loopback
    except ValueError:
        return False


def local_candidates():
    """Find bounded local IPv4 ranges; never scan the wider internet."""
    try:
        result = subprocess.run(["ip", "-j", "-4", "route", "show", "scope", "link"],
                                capture_output=True, text=True, timeout=3, check=False)
        routes = json.loads(result.stdout) if result.returncode == 0 else []
    except (OSError, subprocess.TimeoutExpired, ValueError):
        routes = []
    addresses = set()
    for route in routes:
        device = route.get("dev", "")
        if device.startswith(("docker", "veth", "tailscale", "br-")):
            continue
        try:
            network = ipaddress.ip_network(route.get("dst", ""), strict=False)
        except ValueError:
            continue
        if network.version != 4 or not network.is_private:
            continue
        if network.num_addresses > 512:
            continue
        addresses.update(str(address) for address in network.hosts())
    return sorted(addresses, key=lambda value: tuple(map(int, value.split("."))))


def discover():
    addresses = local_candidates()

    def adb_port_open(host):
        try:
            with socket.create_connection((host, 5555), timeout=0.18):
                return host
        except OSError:
            return None

    with ThreadPoolExecutor(max_workers=64) as pool:
        found = [host for host in pool.map(adb_port_open, addresses) if host]
    return {"ok": True, "hosts": found, "scanned": len(addresses)}


def pair(host):
    if not valid_host(host):
        return {"ok": False, "status": "invalid-host", "error": "Enter a local IPv4 address"}
    serial = host + ":5555"
    try:
        attempt = subprocess.run(["adb", "connect", serial], capture_output=True,
                                 text=True, timeout=7, check=False)
    except FileNotFoundError:
        return {"ok": False, "status": "missing-adb", "error": "Install android-tools"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "status": "offline", "error": "Connection timed out"}
    state, error = adb(serial, "get-state", timeout=3)
    if state is None or state.strip() != "device":
        message = (error or attempt.stdout or attempt.stderr).lower()
        if "unauthorized" in message:
            return {"ok": False, "status": "unauthorized", "host": host,
                    "error": "Allow the debugging prompt on the TV, then connect again"}
        return {"ok": False, "status": "offline", "host": host,
                "error": "Check ADB Debugging and the TV's IP address"}
    manufacturer, error = adb(serial, "shell", "getprop", "ro.product.manufacturer")
    model, error2 = adb(serial, "shell", "getprop", "ro.product.model")
    if manufacturer is None or model is None:
        return {"ok": False, "status": "offline", "error": error or error2 or "Device unavailable"}
    if manufacturer.strip().lower() != "amazon" or not model.strip().upper().startswith("AFT"):
        return {"ok": False, "status": "not-fire-tv", "error": "This is not an Amazon Fire TV"}
    return {"ok": True, "status": "connected", "host": host, "model": model.strip()}


def installed_apps(serial):
    output, error = adb(serial, "shell", "pm", "list", "packages", timeout=8)
    if output is None:
        return {"ok": False, "error": error or "Device unavailable", "apps": []}
    installed = set(re.findall(r"^package:(\S+)$", output, re.M))
    return {"ok": True, "apps": [
        {"package": package, "name": APP_NAMES[package]}
        for package in SHORTCUT_PACKAGES if package in installed
    ]}


def read_history():
    try:
        data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def write_history(entries):
    HISTORY_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".history-", dir=HISTORY_PATH.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(entries[-100:], stream, ensure_ascii=False)
        os.chmod(temporary, 0o600)
        os.replace(temporary, HISTORY_PATH)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def update_history(result):
    entries = read_history()
    if result.get("status") in ("playing", "paused") and result.get("title"):
        key = (result.get("package"), result["title"])
        previous = (entries[-1].get("package"), entries[-1].get("title")) if entries else None
        if key != previous:
            try:
                suppressed_key = HISTORY_BASELINE_PATH.read_text(encoding="utf-8")
            except OSError:
                suppressed_key = ""
            if not entries and suppressed_key == "\n".join(key):
                return []
            HISTORY_BASELINE_PATH.unlink(missing_ok=True)
            entries.append({"at": datetime.now(timezone.utc).isoformat(),
                            "app": result.get("app", ""),
                            "package": key[0], "title": key[1]})
            write_history(entries)
    return entries[-10:][::-1]


def reconnect(serial):
    try:
        subprocess.run(["adb", "disconnect", serial], capture_output=True,
                       text=True, timeout=4, check=False)
        attempt = subprocess.run(["adb", "connect", serial], capture_output=True,
                                 text=True, timeout=6, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"ok": False, "error": str(error)}
    state, error = adb(serial, "get-state", timeout=3)
    return {"ok": state is not None and state.strip() == "device",
            "error": error or (attempt.stderr.strip() if attempt.returncode else "")}


def sessions(text):
    """Parse the session blocks emitted by this Fire OS 7 device."""
    found = []
    current = None
    for line in text.splitlines():
        header = re.match(r"^    \S.*\s(\S+)/(\S+) \(userId=\d+\)$", line)
        if header:
            if current:
                found.append(current)
            current = {"package": header.group(1), "active": False,
                       "state": 0, "title": "", "artist": "", "album": "",
                       "artworkUri": "", "actions": 0}
            continue
        if not current:
            continue
        if line.startswith("    ") and not line.startswith("      "):
            found.append(current)
            current = None
            continue
        stripped = line.strip()
        if stripped == "active=true":
            current["active"] = True
        elif stripped.startswith("state=PlaybackState"):
            match = re.search(r"\bstate=(\d+)", stripped)
            if match:
                current["state"] = int(match.group(1))
            match = re.search(r"\bactions=(-?\d+)", stripped)
            if match:
                current["actions"] = int(match.group(1))
        elif stripped.startswith("metadata:"):
            match = re.search(r"\bdescription=(.*)$", stripped)
            if match:
                parts = match.group(1).rsplit(", ", 2)
                title = parts[0].strip()
                if title.lower() != "null":
                    current["title"] = title
                if len(parts) == 3:
                    current["artist"] = "" if parts[1].lower() == "null" else parts[1]
                    current["album"] = "" if parts[2].lower() == "null" else parts[2]
            match = re.search(r"\b(?:artUri|artworkUri)=([^,\s]+)", stripped)
            if match and match.group(1).lower() != "null":
                current["artworkUri"] = match.group(1)
    if current:
        found.append(current)
    return found


def foreground_package(text):
    match = re.search(r"mCurrentFocus=Window\{[^\n]*\s([\w.]+)/[^\s}]+", text)
    return match.group(1) if match else ""


def app_icon_path(package):
    path = APP_ICON_PATHS.get(package, "")
    return path if path and Path(path).is_file() else ""


def snapshot(serial):
    state, error = adb(serial, "get-state", timeout=3)
    if state is None or state.strip() != "device":
        if "unauthorized" in (error or "").lower():
            return {"status": "unauthorized"}
        try:
            subprocess.run(["adb", "connect", serial], capture_output=True,
                           text=True, timeout=4, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
        state, error = adb(serial, "get-state", timeout=3)
        if state is None or state.strip() != "device":
            return {"status": "offline"}

    media, error = adb(serial, "shell", "dumpsys", "media_session")
    if media is None:
        return {"status": "offline"}
    window, error = adb(serial, "shell", "dumpsys", "window", "windows")
    if window is None:
        return {"status": "offline"}

    if re.search(r"mCurrentFocus=Window\{[^\n]*:dream\}", window):
        return {"status": "screensaver", "app": "Fire TV"}

    package = foreground_package(window)
    if not package:
        return {"status": "unknown"}
    if package == "com.amazon.tv.launcher":
        return {"status": "home", "app": "Fire TV Home"}

    candidates = [s for s in sessions(media) if s["package"] == package and s["active"]]
    playing = next((s for s in candidates if s["state"] == 3), None)
    paused = next((s for s in candidates if s["state"] == 2), None)
    selected = playing or paused
    reported_title = selected["title"] if selected else ""
    if package == "com.amazon.firebat" and reported_title == "PrimeVideo":
        reported_title = ""
    return {
        "status": "playing" if playing else "paused" if paused else "app",
        "app": APP_NAMES.get(package, package.split(".")[-1]),
        "package": package,
        "appIconPath": app_icon_path(package),
        "title": reported_title,
        "artist": selected["artist"] if selected else "",
        "album": selected["album"] if selected else "",
        "artworkUri": selected["artworkUri"] if selected else "",
        "actions": selected["actions"] if selected else 0,
    }


def control(serial, requested):
    if requested == "reconnect":
        return reconnect(serial)
    if requested == "clear-history":
        try:
            HISTORY_PATH.unlink(missing_ok=True)
            current = snapshot(serial)
            if current.get("status") in ("playing", "paused") and current.get("title"):
                HISTORY_BASELINE_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                HISTORY_BASELINE_PATH.write_text(
                    current.get("package", "") + "\n" + current["title"], encoding="utf-8")
                HISTORY_BASELINE_PATH.chmod(0o600)
            else:
                HISTORY_BASELINE_PATH.unlink(missing_ok=True)
            return {"ok": True, "command": requested}
        except OSError as error:
            return {"ok": False, "error": str(error)}
    if requested in REMOTE_KEYS:
        result, error = adb(serial, "shell", "input", "keyevent",
                            REMOTE_KEYS[requested], timeout=5)
        return {"ok": result is not None, "error": error or "",
                "command": requested}

    current = snapshot(serial)
    status = current.get("status")
    actions = current.get("actions", 0)
    commands = {
        "previous": (ACTION_PREVIOUS, "previous"),
        "next": (ACTION_NEXT, "next"),
        "rewind": (ACTION_REWIND, "rewind"),
        "fast-forward": (ACTION_FAST_FORWARD, "fast-forword"),
    }
    if requested == "play-pause":
        if status == "playing":
            required, command = ACTION_PAUSE, "pause"
        elif status == "paused":
            required, command = ACTION_PLAY, "play"
        else:
            return {"ok": False, "error": "No active playback"}
    elif requested in commands:
        required, command = commands[requested]
    else:
        return {"ok": False, "error": "Unsupported command"}

    if status not in ("playing", "paused") or not actions & required:
        return {"ok": False, "error": "Action unavailable"}
    result, error = adb(serial, "shell", "media", "dispatch", command,
                        timeout=5)
    if result is None:
        return {"ok": False, "error": error or "Command failed"}
    return {"ok": True, "command": command}


def launch(serial, package):
    if package not in SHORTCUT_PACKAGES:
        return {"ok": False, "error": "App shortcut unavailable"}
    apps = installed_apps(serial)
    if not apps["ok"]:
        return apps
    if package not in {app["package"] for app in apps["apps"]}:
        return {"ok": False, "error": "App is not installed"}
    if package == "com.amazon.firebat":
        output, error = adb(serial, "shell", "am", "start", "-n",
                            "com.amazon.firebat/com.amazon.pyrocore.IgnitionActivity", timeout=8)
        return {"ok": output is not None, "error": error or "",
                "app": APP_NAMES[package]}
    output, error = adb(serial, "shell", "monkey", "-p", package,
                        "-c", "android.intent.category.LAUNCHER", "1", timeout=8)
    if output is None or "Events injected: 1" not in output:
        return {"ok": False, "error": error or "App did not launch"}
    return {"ok": True, "app": APP_NAMES[package]}


def type_text(serial, value):
    if not re.fullmatch(r"[A-Za-z0-9 .,!?@:_-]{1,100}", value):
        return {"ok": False, "error": "Use up to 100 basic Latin characters"}
    encoded = value.replace(" ", "%s")
    output, error = adb(serial, "shell", "input", "text", shlex.quote(encoded), timeout=8)
    return {"ok": output is not None, "error": error or ""}


def main():
    if len(sys.argv) == 2 and sys.argv[1] == "--discover":
        print(json.dumps(discover(), ensure_ascii=False))
        return
    if len(sys.argv) == 3 and sys.argv[1] == "--pair":
        print(json.dumps(pair(sys.argv[2]), ensure_ascii=False))
        return
    if len(sys.argv) not in (2, 3, 4) or not valid_host(sys.argv[1]):
        if sys.argv[1] == "":
            print(json.dumps({"status": "not-configured"}))
            return
        print(json.dumps({"status": "invalid-host"}))
        return
    serial = sys.argv[1] + ":5555"
    if len(sys.argv) == 3 and sys.argv[2] == "--apps":
        print(json.dumps(installed_apps(serial), ensure_ascii=False))
        return
    if len(sys.argv) == 4:
        if sys.argv[2] == "--control":
            result = control(serial, sys.argv[3])
        elif sys.argv[2] == "--launch":
            result = launch(serial, sys.argv[3])
        elif sys.argv[2] == "--type":
            result = type_text(serial, sys.argv[3])
        elif sys.argv[2] == "--history":
            started = time.monotonic()
            result = snapshot(serial)
            result["latencyMs"] = round((time.monotonic() - started) * 1000)
            result["history"] = update_history(result) if sys.argv[3] == "on" else []
            result["checkedAt"] = datetime.now(timezone.utc).isoformat()
            print(json.dumps(result, ensure_ascii=False))
            return
        else:
            print(json.dumps({"ok": False, "error": "Invalid arguments"}))
            return
        print(json.dumps(result, ensure_ascii=False))
        return
    started = time.monotonic()
    result = snapshot(serial)
    result["latencyMs"] = round((time.monotonic() - started) * 1000)
    result["checkedAt"] = datetime.now(timezone.utc).isoformat()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
