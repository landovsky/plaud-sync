"""Finding the phone over adb — cable first, then Wi-Fi.

Shared by the phone-photo pull (visuals.py) and the Google Recorder listing
(google_recorder.py), so both see the phone whichever way it is attached.
Wireless debugging picks a new port every time it is toggled, so the address
can't be configured: it is discovered through adb's mDNS browser.
"""
import subprocess
import time

CONNECT_SERVICE = "_adb-tls-connect._tcp"

# adb browses mDNS in the background, so the first `mdns services` after the
# server starts (or after the phone wakes) is often empty.
MDNS_TRIES = 4
MDNS_WAIT_S = 2

PAIR_HINT = ("  Phone found over Wi-Fi but it refused the connection — it is probably not "
             "paired with this Mac yet.\n"
             "  Once: phone → Developer options → Wireless debugging → Pair device with "
             "pairing code, then run  adb pair <ip>:<port>  and enter the code.")


def _device_lines(adb):
    try:
        devs = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=20).stdout
    except (subprocess.SubprocessError, OSError):
        return []
    return [l for l in devs.splitlines()[1:] if l.strip()]


def device_state(adb):
    """'ready' | 'unauthorized' | 'absent' — the three cases worth telling apart."""
    lines = _device_lines(adb)
    if any(l.split()[1:2] == ["device"] for l in lines):
        return "ready"
    if any("unauthorized" in l for l in lines):
        return "unauthorized"
    return "absent"


def _wifi_addresses(adb):
    """host:port of every phone advertising wireless debugging, waiting a few
    seconds for the browse to deliver one."""
    for attempt in range(MDNS_TRIES):
        if attempt:
            time.sleep(MDNS_WAIT_S)
        found = _mdns_connect_services(adb)
        if found:
            return found
    return []


def _mdns_connect_services(adb):
    try:
        out = subprocess.run([adb, "mdns", "services"], capture_output=True, text=True,
                             timeout=20).stdout
    except (subprocess.SubprocessError, OSError):
        return []
    found = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1] == CONNECT_SERVICE and parts[2] not in found:
            found.append(parts[2])
    return found


def connect_wifi(adb, hint=None):
    """Try `adb connect` to every phone advertised over Wi-Fi. `hint` is called
    with the pairing instructions if a phone is advertised but refuses us."""
    refused = False
    for addr in _wifi_addresses(adb):
        try:
            out = subprocess.run([adb, "connect", addr], capture_output=True, text=True,
                                 timeout=30).stdout
        except (subprocess.SubprocessError, OSError):
            continue
        if not out.startswith(("connected to", "already connected")):
            refused = True
        elif sum(l.split()[1:2] == ["device"] for l in _device_lines(adb)) > 1:
            # adb's own mDNS auto-connect got there first. The same phone on two
            # transports makes every plain `adb shell`/`pull` fail with "more than
            # one device", so drop the one we added.
            subprocess.run([adb, "disconnect", addr], capture_output=True, timeout=20)
    if refused and hint and device_state(adb) == "absent":
        hint(PAIR_HINT)


def find_phone(adb, hint=None):
    """device_state(), after trying Wi-Fi if the cable didn't deliver a phone.
    An 'unauthorized' answer is kept as is: the phone is there, it just needs a tap."""
    state = device_state(adb)
    if state == "ready":
        return state
    connect_wifi(adb, hint)
    now = device_state(adb)
    return now if now != "absent" else state
