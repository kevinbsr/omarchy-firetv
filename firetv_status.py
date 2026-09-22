#!/usr/bin/env python3
"""Read foreground app and media session from a Fire OS TV over local ADB."""

import hashlib
import http.client
import io
import ipaddress
import json
import os
import re
import resource
import selectors
import shlex
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


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
    "com.tailscale.ipn": "Tailscale",
    "com.amazon.tv.launcher": "Fire TV Home",
}

SHORTCUT_PACKAGES = tuple(package for package in APP_NAMES
                          if package not in ("com.amazon.tv.launcher", "com.tailscale.ipn"))
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
ARTWORK_DIR = Path.home() / ".cache/omarchy/firetv/artwork"
ADB = "/usr/bin/adb"
IP = "/usr/bin/ip"
MAGICK = "/usr/bin/magick"
MAX_JSON_BYTES = 16 * 1024
MAX_MEDIA_BYTES = 2 * 1024 * 1024
MAX_WINDOW_BYTES = 512 * 1024
MAX_PACKAGES_BYTES = 512 * 1024
MAX_LINE_BYTES = 4096
MAX_ARTWORK_BYTES = 1024 * 1024
CHILD_ENV = {"HOME": str(Path.home()), "PATH": "/usr/bin", "LANG": "C.UTF-8"}

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


class OutputLimitExceeded(Exception):
    pass


def bounded_lines(value, max_lines=20000):
    for count, line in enumerate(io.StringIO(value), 1):
        if count > max_lines or len(line.encode("utf-8")) > MAX_LINE_BYTES:
            raise OutputLimitExceeded("Device output exceeded parsing limit")
        yield line.rstrip("\r\n")


def bounded_field(value, max_chars):
    if len(value) > max_chars or any(ord(char) < 32 for char in value):
        raise OutputLimitExceeded("Device field exceeded parsing limit")
    return value


active_process = None


def stop_process(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def terminate_helper(signum, frame):
    if active_process is not None:
        stop_process(active_process)
    raise SystemExit(128 + signum)


signal.signal(signal.SIGTERM, terminate_helper)


def bounded_run(command, timeout=8, max_bytes=64 * 1024, limits=None):
    """Drain both pipes incrementally; reap the whole child group on failure."""
    global active_process
    def apply_limits():
        if limits:
            for kind, value in limits.items():
                resource.setrlimit(kind, (value, value))

    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env=CHILD_ENV, start_new_session=True,
                               preexec_fn=apply_limits if limits else None)
    active_process = process
    selector = selectors.DefaultSelector()
    chunks = {process.stdout: [], process.stderr: []}
    total = 0
    deadline = time.monotonic() + timeout
    try:
        for pipe in chunks:
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            for key, _ in selector.select(remaining):
                block = os.read(key.fileobj.fileno(), min(8192, max_bytes - total + 1))
                if not block:
                    selector.unregister(key.fileobj)
                    continue
                total += len(block)
                if total > max_bytes:
                    raise OutputLimitExceeded("Device output exceeded safety limit")
                chunks[key.fileobj].append(block)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(command, timeout)
        returncode = process.wait(timeout=remaining)
        return subprocess.CompletedProcess(
            command, returncode,
            b"".join(chunks[process.stdout]).decode("utf-8", "replace"),
            b"".join(chunks[process.stderr]).decode("utf-8", "replace"))
    except BaseException:
        stop_process(process)
        raise
    finally:
        selector.close()
        for pipe in chunks:
            pipe.close()
        if active_process is process:
            active_process = None


def adb(serial, *args, timeout=8, max_bytes=64 * 1024):
    try:
        result = bounded_run([ADB, "-s", serial, *args], timeout, max_bytes)
    except (OSError, subprocess.TimeoutExpired, OutputLimitExceeded) as error:
        message = "Device output exceeded safety limit" if isinstance(error, OutputLimitExceeded) else str(error)[:160]
        return None, message
    if result.returncode:
        return None, (result.stderr or result.stdout).strip()[:160]
    return result.stdout, None


def valid_host(value):
    try:
        address = ipaddress.ip_address(value)
        tailscale_range = ipaddress.ip_network("100.64.0.0/10")
        return (address.version == 4 and not address.is_loopback
                and (address.is_private or address in tailscale_range))
    except ValueError:
        return False


def local_candidates():
    """Find bounded local IPv4 ranges; never scan the wider internet."""
    try:
        result = bounded_run([IP, "-j", "-4", "route", "show", "scope", "link"], 3, 64 * 1024)
        routes = json.loads(result.stdout) if result.returncode == 0 else []
        if not isinstance(routes, list):
            routes = []
    except (OSError, subprocess.TimeoutExpired, OutputLimitExceeded, ValueError):
        routes = []
    addresses = set()
    for route in routes:
        if not isinstance(route, dict):
            continue
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
        if len(addresses) >= 512:
            break
    return sorted(addresses, key=lambda value: tuple(map(int, value.split("."))))[:512]


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
        return {"ok": False, "status": "invalid-host", "error": "Enter a LAN or Tailscale IPv4 address"}
    serial = host + ":5555"
    try:
        attempt = bounded_run([ADB, "connect", serial], 7)
    except FileNotFoundError:
        return {"ok": False, "status": "missing-adb", "error": "Install android-tools"}
    except (subprocess.TimeoutExpired, OutputLimitExceeded):
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
    try:
        manufacturer = bounded_field(manufacturer.strip(), 80)
        model = bounded_field(model.strip(), 80)
    except OutputLimitExceeded:
        return {"ok": False, "status": "offline", "error": "Device output exceeded safety limit"}
    if manufacturer.lower() != "amazon" or not model.upper().startswith("AFT"):
        return {"ok": False, "status": "not-fire-tv", "error": "This is not an Amazon Fire TV"}
    return {"ok": True, "status": "connected", "host": host, "model": model}


def installed_apps(serial):
    output, error = adb(serial, "shell", "pm", "list", "packages", timeout=8,
                        max_bytes=MAX_PACKAGES_BYTES)
    if output is None:
        return {"ok": False, "error": error or "Device unavailable", "apps": []}
    try:
        installed = set()
        for line in bounded_lines(output, 10000):
            if line.startswith("package:"):
                installed.add(bounded_field(line[8:], 160))
    except OutputLimitExceeded:
        return {"ok": False, "error": "Device output exceeded safety limit", "apps": []}
    return {"ok": True, "apps": [
        {"package": package, "name": APP_NAMES[package]}
        for package in SHORTCUT_PACKAGES if package in installed
    ]}


def read_history():
    try:
        if HISTORY_PATH.stat().st_size > 128 * 1024:
            return []
        data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []
        return [{key: bounded_field(str(item.get(key, "")), limit)
                 for key, limit in (("at", 64), ("app", 160), ("package", 160), ("title", 512))}
                for item in data[-100:] if isinstance(item, dict)]
    except (OSError, ValueError, OutputLimitExceeded):
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
        bounded_run([ADB, "disconnect", serial], 4)
        attempt = bounded_run([ADB, "connect", serial], 6)
    except (OSError, subprocess.TimeoutExpired, OutputLimitExceeded) as error:
        return {"ok": False, "error": str(error)}
    state, error = adb(serial, "get-state", timeout=3)
    return {"ok": state is not None and state.strip() == "device",
            "error": error or (attempt.stderr.strip() if attempt.returncode else "")}


def sessions(text):
    """Parse the session blocks emitted by this Fire OS 7 device."""
    found = []
    current = None
    for line in bounded_lines(text):
        header = re.match(r"^    \S.*\s(\S+)/(\S+) \(userId=\d+\)$", line)
        if header:
            if current:
                found.append(current)
            if len(found) >= 128:
                raise OutputLimitExceeded("Too many media sessions")
            current = {"package": bounded_field(header.group(1), 160), "active": False,
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
                    current["title"] = bounded_field(title, 512)
                if len(parts) == 3:
                    current["artist"] = "" if parts[1].lower() == "null" else bounded_field(parts[1], 256)
                    current["album"] = "" if parts[2].lower() == "null" else bounded_field(parts[2], 256)
            match = re.search(r"\b(?:artUri|artworkUri)=([^,\s]+)", stripped)
            if match and match.group(1).lower() != "null":
                current["artworkUri"] = bounded_field(match.group(1), 2048)
    if current:
        found.append(current)
    return found


def foreground_package(text):
    for line in bounded_lines(text, 12000):
        if "mCurrentFocus=Window{" in line:
            match = re.search(r"mCurrentFocus=Window\{[^\n]*\s([\w.]+)/[^\s}]+", line)
            return bounded_field(match.group(1), 160) if match else ""
    return ""


def app_icon_path(package):
    path = APP_ICON_PATHS.get(package, "")
    return path if path and Path(path).is_file() else ""


def artwork_target(uri):
    """Only HTTPS on the default port to a globally routable address."""
    if not uri or len(uri) > 2048 or any(ord(char) < 33 for char in uri):
        return None
    try:
        parsed = urlsplit(uri)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or
                parsed.password or parsed.fragment or parsed.port not in (None, 443)):
            return None
        host = parsed.hostname.encode("idna").decode("ascii")
        if len(host) > 253:
            return None
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        ips = [entry[4][0] for entry in addresses]
        if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
            return None
        path = (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
        return host, ips[0], path
    except (ValueError, UnicodeError, OSError):
        return None


def artwork_deadline(signum, frame):
    raise TimeoutError("Artwork deadline exceeded")


def fetch_artwork(uri):
    signal.signal(signal.SIGALRM, artwork_deadline)
    signal.setitimer(signal.ITIMER_REAL, 6)
    try:
        return _fetch_artwork(uri)
    except (TimeoutError, OSError, ValueError, ssl.SSLError,
            http.client.HTTPException, subprocess.TimeoutExpired, OutputLimitExceeded):
        return ""
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


def _fetch_artwork(uri):
    """Decode remote artwork to a small local PNG; fail closed to the app icon."""
    target = artwork_target(uri)
    if not target or not Path(MAGICK).is_file():
        return ""
    host, ip, path = target
    name = hashlib.sha256(uri.encode("utf-8")).hexdigest()[:24] + ".png"
    ARTWORK_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    ARTWORK_DIR.chmod(0o700)
    cached = ARTWORK_DIR / name
    if cached.is_file() and 0 < cached.stat().st_size <= 512 * 1024:
        return name
    connection = http.client.HTTPSConnection(host, 443, timeout=2,
                                              context=ssl.create_default_context())
    connection._create_connection = lambda address, timeout=None, source_address=None: \
        socket.create_connection((ip, 443), timeout=min(timeout or 2, 2))
    started = time.monotonic()
    try:
        connection.request("GET", path, headers={"Host": host, "Accept": "image/png, image/jpeg, image/webp",
                                                 "Accept-Encoding": "identity", "User-Agent": "omarchy-firetv/1"})
        response = connection.getresponse()
        kind = response.getheader("Content-Type", "").split(";", 1)[0].lower().strip()
        length = response.getheader("Content-Length", "")
        if (response.status != 200 or kind not in ("image/png", "image/jpeg", "image/webp") or
                response.getheader("Content-Encoding", "identity").lower() != "identity" or
                (length and (not length.isdecimal() or int(length) > MAX_ARTWORK_BYTES))):
            return ""
        with tempfile.TemporaryDirectory(prefix=".fetch-", dir=ARTWORK_DIR) as temporary:
            extension = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[kind]
            source = Path(temporary) / ("input." + extension)
            output = Path(temporary) / "output.png"
            size = 0
            with source.open("wb") as stream:
                while True:
                    remaining = 4 - (time.monotonic() - started)
                    if remaining <= 0:
                        return ""
                    if connection.sock:
                        connection.sock.settimeout(min(1, remaining))
                    chunk = response.read(min(64 * 1024, MAX_ARTWORK_BYTES - size + 1))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_ARTWORK_BYTES:
                        return ""
                    stream.write(chunk)
            with source.open("rb") as stream:
                header = stream.read(16)
            magic = ((kind == "image/png" and header.startswith(b"\x89PNG\r\n\x1a\n")) or
                     (kind == "image/jpeg" and header.startswith(b"\xff\xd8\xff")) or
                     (kind == "image/webp" and header[:4] == b"RIFF" and header[8:12] == b"WEBP"))
            if not magic:
                return ""
            result = bounded_run([MAGICK, "-limit", "memory", "64MiB", "-limit", "map", "64MiB",
                                  "-limit", "disk", "0", "-limit", "width", "1024",
                                  "-limit", "height", "1024", "-limit", "area", "1MP",
                                  extension + ":" + str(source), "-thumbnail", "256x256",
                                  "-strip", "png:" + str(output)], 3, 4096,
                                 {resource.RLIMIT_AS: 256 * 1024 * 1024,
                                  resource.RLIMIT_CPU: 2, resource.RLIMIT_FSIZE: 1024 * 1024})
            if result.returncode or not output.is_file() or not (0 < output.stat().st_size <= 512 * 1024):
                return ""
            output.chmod(0o600)
            os.replace(output, cached)
        old = sorted(ARTWORK_DIR.glob("[0-9a-f]*.png"), key=lambda item: item.stat().st_mtime)
        for item in old[:-16]:
            item.unlink(missing_ok=True)
        return name
    except (OSError, ValueError, ssl.SSLError, http.client.HTTPException,
            subprocess.TimeoutExpired, OutputLimitExceeded):
        return ""
    finally:
        connection.close()


def snapshot(serial):
    state, error = adb(serial, "get-state", timeout=3)
    if state is None or state.strip() != "device":
        if "unauthorized" in (error or "").lower():
            return {"status": "unauthorized"}
        try:
            bounded_run([ADB, "connect", serial], 4)
        except (OSError, subprocess.TimeoutExpired, OutputLimitExceeded):
            pass
        state, error = adb(serial, "get-state", timeout=3)
        if state is None or state.strip() != "device":
            return {"status": "offline"}

    media, error = adb(serial, "shell", "dumpsys", "media_session",
                       max_bytes=MAX_MEDIA_BYTES)
    if media is None:
        return {"status": "offline"}
    window, error = adb(serial, "shell", "dumpsys", "window", "windows",
                        max_bytes=MAX_WINDOW_BYTES)
    if window is None:
        return {"status": "offline"}

    try:
        if re.search(r"mCurrentFocus=Window\{[^\n]*:dream\}", window):
            return {"status": "screensaver", "app": "Fire TV"}
        package = foreground_package(window)
        parsed_sessions = sessions(media)
    except OutputLimitExceeded:
        return {"status": "offline", "error": "Device output exceeded safety limit"}
    if not package:
        return {"status": "unknown"}
    if package == "com.amazon.tv.launcher":
        return {"status": "home", "app": "Fire TV Home"}

    candidates = [s for s in parsed_sessions if s["package"] == package and s["active"]]
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
        "artworkFile": fetch_artwork(selected["artworkUri"]) if selected else "",
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
        "fast-forward": (ACTION_FAST_FORWARD, "fast-forward"),
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


def emit(result):
    encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_JSON_BYTES:
        encoded = b'{"ok":false,"status":"offline","error":"Response exceeded safety limit"}'
    sys.stdout.buffer.write(encoded + b"\n")


def main():
    if len(sys.argv) == 1:
        emit({"ok": False, "error": "Usage: firetv_status.py HOST [COMMAND]"})
        return
    if len(sys.argv) == 2 and sys.argv[1] == "--discover":
        emit(discover())
        return
    if len(sys.argv) == 3 and sys.argv[1] == "--pair":
        emit(pair(sys.argv[2]))
        return
    if len(sys.argv) not in (2, 3, 4) or not valid_host(sys.argv[1]):
        if sys.argv[1] == "":
            emit({"status": "not-configured"})
            return
        emit({"status": "invalid-host"})
        return
    serial = sys.argv[1] + ":5555"
    if len(sys.argv) == 3 and sys.argv[2] == "--apps":
        emit(installed_apps(serial))
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
            emit(result)
            return
        else:
            emit({"ok": False, "error": "Invalid arguments"})
            return
        emit(result)
        return
    started = time.monotonic()
    result = snapshot(serial)
    result["latencyMs"] = round((time.monotonic() - started) * 1000)
    result["checkedAt"] = datetime.now(timezone.utc).isoformat()
    emit(result)


if __name__ == "__main__":
    main()
