"""Finding a paired PC again under any of its addresses (Viktor 27.11.).

Until ``0.7.5-dev.2`` a peer was dialed only at the one ``host`` stored in its
entry. On BOOK-Viktor that was the LAN address of the other PC; over Tailscale
or WireGuard, or after the other PC got another address, the connector kept
dialing it (7597 identical ``outbound connect failed ... timed out`` lines in
one night) and never found the PC.

A peer is kept by its device ID (``device:<id>``, ``runtime_model``). This
module adds what the connector needs to reach it:

- ``remember_address``: every address a connection worked over is kept in the
  entry (``peer["addresses"]``, newest first, at most ``MAX_ADDRESSES``), with
  the way (LAN, Tailscale, VPN) and the time.
- ``candidate_addresses``: the order the connector tries: the address that
  worked last, the address typed in, the other known ones, then the device
  of the same name in ``tailscale status`` (when Tailscale runs).
- ``ReachTracker``: per peer the last working address and why it is not
  connected now (time, one sentence), for the Peers page; and the log lines,
  with repeated equal failures summarized instead of one line per attempt.

Pure module (standard library only, no Windows imports), unit-testable on any OS.
"""
from __future__ import annotations

import errno
import ipaddress
import socket
import threading
import time

DEFAULT_PORT = 45781
MAX_ADDRESSES = 6
# Equal failures in a row are summarized in one log line this often.
SUMMARY_INTERVAL = 600.0

VIA_LAN = "lan"
VIA_TAILSCALE = "tailscale"
VIA_VPN = "vpn"
VIA_LABELS = {VIA_LAN: "LAN", VIA_TAILSCALE: "Tailscale", VIA_VPN: "VPN"}

SOURCE_LAST_OK = "last_ok"
SOURCE_CONFIGURED = "configured"
SOURCE_KNOWN = "known"
SOURCE_TAILSCALE = "tailscale_status"

_CGNAT = ipaddress.ip_network("100.64.0.0/10")


# ── Addresses ───────────────────────────────────────────────────────
def _port(value, default=DEFAULT_PORT):
    try:
        port = int(value or default)
    except (TypeError, ValueError):
        return default
    return port if 0 < port < 65536 else default


def _clean_host(value):
    host = str(value or "").strip()
    if not host or len(host) > 253 or any(ch.isspace() for ch in host):
        return ""
    return host


def classify_via(host):
    """``tailscale`` for a 100.64.0.0/10 address or a ``.ts.net`` name, else ``lan``."""
    host = _clean_host(host).lower()
    if host.endswith(".ts.net") or host.endswith(".ts.net."):
        return VIA_TAILSCALE
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return VIA_LAN
    if address.version == 4 and address in _CGNAT:
        return VIA_TAILSCALE
    return VIA_LAN


def via_label(via):
    return VIA_LABELS.get(via, VIA_LABELS[VIA_LAN])


def known_addresses(peer):
    """The remembered addresses of an entry, checked, newest success first."""
    rows = []
    seen = set()
    raw = peer.get("addresses") if isinstance(peer, dict) else None
    for item in raw if isinstance(raw, list) else ():
        if not isinstance(item, dict):
            continue
        host = _clean_host(item.get("host"))
        if not host:
            continue
        port = _port(item.get("port"), _port(peer.get("port")))
        key = (host.lower(), port)
        if key in seen:
            continue
        seen.add(key)
        via = item.get("via") if item.get("via") in VIA_LABELS else classify_via(host)
        try:
            last_ok = float(item.get("last_ok") or 0)
        except (TypeError, ValueError):
            last_ok = 0.0
        rows.append({"host": host, "port": port, "via": via, "last_ok": int(max(0.0, last_ok))})
        if len(rows) >= MAX_ADDRESSES:
            break
    rows.sort(key=lambda r: r["last_ok"], reverse=True)
    return rows


def remember_address(peer, host, port, via=None, now=None):
    """Note that a connection to ``host``:``port`` worked; changes ``peer`` in place.

    Returns True when the stored list changed in a way worth saving (a new
    address, or another address than before is now the newest); refreshing
    the time of the address that already was the newest returns False.
    """
    host = _clean_host(host)
    if not isinstance(peer, dict) or not host:
        return False
    port = _port(port, _port(peer.get("port")))
    via = via if via in VIA_LABELS else classify_via(host)
    now = int(time.time() if now is None else now)
    before = known_addresses(peer)
    rest = [r for r in before if (r["host"].lower(), r["port"]) != (host.lower(), port)]
    rows = [{"host": host, "port": port, "via": via, "last_ok": now}] + rest
    rows = rows[:MAX_ADDRESSES]
    peer["addresses"] = rows
    if not before:
        return True
    first = before[0]
    return (first["host"].lower(), first["port"]) != (host.lower(), port) or first["via"] != via


def _peer_names(peer):
    names = set()
    for key in ("name", "display_name", "host"):
        value = str(peer.get(key) or "").strip().lower().rstrip(".")
        if not value:
            continue
        try:
            ipaddress.ip_address(value)
            continue
        except ValueError:
            pass
        names.add(value)
        names.add(value.split(".", 1)[0])
    return names


def tailscale_address_for(peer, status):
    """The 100.x address of the same PC in ``tailscale status --json``, or None.

    Matched by name: the peer's name, its announced name or a host name typed
    in, against the Tailscale host name and the first part of its DNS name
    (case does not matter). Offline devices are skipped.
    """
    if not isinstance(peer, dict) or not isinstance(status, dict):
        return None
    nodes = status.get("Peer")
    if not isinstance(nodes, dict):
        return None
    names = _peer_names(peer)
    if not names:
        return None
    for node in nodes.values():
        if not isinstance(node, dict) or node.get("Online") is False:
            continue
        host_name = str(node.get("HostName") or "").strip().lower()
        dns_name = str(node.get("DNSName") or "").strip().lower().rstrip(".")
        if not ({host_name, dns_name.split(".", 1)[0], dns_name} & names):
            continue
        for value in node.get("TailscaleIPs") or []:
            try:
                address = ipaddress.ip_address(str(value).strip())
            except ValueError:
                continue
            if address.version == 4:
                return {"host": str(address), "name": dns_name or host_name}
    return None


def candidate_addresses(peer, tailscale_status=None):
    """``[{"host", "port", "via", "source"}]`` in the order the connector tries them.

    1. the address the last connection worked over, 2. the address in the
    entry (typed in or found by the scan), 3. the other remembered ones, 4.
    the device of the same name in Tailscale (only with ``tailscale_status``).
    No address twice.
    """
    if not isinstance(peer, dict):
        return []
    port = _port(peer.get("port"))
    out = []
    seen = set()

    def add(host, p, via, source):
        host = _clean_host(host)
        if not host:
            return
        key = (host.lower(), p)
        if key in seen:
            return
        seen.add(key)
        out.append({"host": host, "port": p, "via": via or classify_via(host), "source": source})

    known = known_addresses(peer)
    if known and known[0]["last_ok"]:
        add(known[0]["host"], known[0]["port"], known[0]["via"], SOURCE_LAST_OK)
    add(peer.get("host"), port, None, SOURCE_CONFIGURED)
    for row in known:
        add(row["host"], row["port"], row["via"], SOURCE_KNOWN)
    match = tailscale_address_for(peer, tailscale_status)
    if match:
        add(match["host"], port, VIA_TAILSCALE, SOURCE_TAILSCALE)
    return out


# ── Failure reasons ─────────────────────────────────────────────────
REASON_TIMEOUT = "timeout"
REASON_REFUSED = "refused"
REASON_UNREACHABLE = "unreachable"
REASON_UNKNOWN_NAME = "unknown_name"
REASON_CLOSED = "closed"
REASON_ERROR = "error"

_UNREACHABLE = {errno.ENETUNREACH, errno.EHOSTUNREACH, 10051, 10065}

_REASON_TEXT = {
    REASON_TIMEOUT: "no answer (timeout): the PC is off, FlowShift does not run there, "
                    "or a firewall or the network drops the connection",
    REASON_REFUSED: "the PC answers, but FlowShift does not listen on the port",
    REASON_UNREACHABLE: "no way to this address from here (other network, VPN not connected)",
    REASON_UNKNOWN_NAME: "the name is unknown here",
    REASON_CLOSED: "the connection was closed right after it opened",
    REASON_ERROR: "connection error",
}


def reason_code(exc):
    """Short code of a connect error (``timeout``, ``refused``, ...)."""
    if isinstance(exc, str):
        return exc if exc in _REASON_TEXT else REASON_ERROR
    if isinstance(exc, socket.gaierror):
        return REASON_UNKNOWN_NAME
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return REASON_TIMEOUT
    if isinstance(exc, ConnectionRefusedError):
        return REASON_REFUSED
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
        return REASON_CLOSED
    code = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
    if isinstance(exc, OSError) and code in _UNREACHABLE:
        return REASON_UNREACHABLE
    if isinstance(exc, OSError) and code in (errno.ETIMEDOUT, 10060):
        return REASON_TIMEOUT
    return REASON_ERROR


def reason_text(code):
    return _REASON_TEXT.get(code, _REASON_TEXT[REASON_ERROR])


def _address_label(row):
    return f"{row['host']}:{row['port']} ({via_label(row.get('via'))})"


def _clock_text(at):
    return time.strftime("%H:%M:%S", time.localtime(at))


# ── Tracker ─────────────────────────────────────────────────────────
class ReachTracker:
    """Per peer token: last working address, current failure, summarized log lines.

    Methods return ``[(level, text)]`` for the caller to log; the tracker
    itself never logs and never raises on odd input.
    """

    def __init__(self, clock=time.time, summary_interval=SUMMARY_INTERVAL):
        self._clock = clock
        self._interval = float(summary_interval)
        self._lock = threading.Lock()
        self._peers = {}

    def _entry(self, token):
        return self._peers.setdefault(token, {
            "last_ok": None, "failure": None, "failing_since": None,
            "streaks": {}, "failed_rounds": 0, "failed_total": 0,
        })

    def attempt_failed(self, token, name, row, exc):
        """One connect attempt to ``row`` failed. Logs only the first of a streak."""
        code = reason_code(exc)
        now = self._clock()
        lines = []
        with self._lock:
            entry = self._entry(token)
            key = (row["host"].lower(), row["port"])
            streak = entry["streaks"].get(key)
            if streak is None or streak["code"] != code:
                if streak is not None and streak["unlogged"]:
                    lines.append(self._summary_line(name, row, streak))
                streak = {"code": code, "since": now, "count": 0, "unlogged": 0,
                          "logged_at": now}
                entry["streaks"][key] = streak
                lines.append(("INFO", f"peer {name}: connect to {_address_label(row)} failed: "
                                      f"{reason_text(code)}; trying again every few seconds, "
                                      "repeats are summarized"))
            else:
                streak["unlogged"] += 1
                if now - streak["logged_at"] >= self._interval:
                    lines.append(self._summary_line(name, row, streak))
                    streak["unlogged"] = 0
                    streak["logged_at"] = now
            streak["count"] += 1
            entry["failed_total"] += 1
            streak["last_detail"] = str(exc)[:160] if not isinstance(exc, str) else exc
        return lines

    @staticmethod
    def _summary_line(name, row, streak):
        return ("INFO", f"peer {name}: connect to {_address_label(row)} still failing "
                        f"({streak['count']} attempts since {_clock_text(streak['since'])}): "
                        f"{reason_text(streak['code'])}")

    def round_failed(self, token, name, tried):
        """Every candidate of one round failed: remember why, for the Peers page."""
        now = self._clock()
        with self._lock:
            entry = self._entry(token)
            if entry["failing_since"] is None:
                entry["failing_since"] = now
            entry["failed_rounds"] += 1
            parts = []
            for row in tried:
                streak = entry["streaks"].get((row["host"].lower(), row["port"]))
                code = streak["code"] if streak else REASON_ERROR
                parts.append({"host": row["host"], "port": row["port"], "via": row.get("via"),
                              "reason": code})
            entry["failure"] = {"at": now, "tried": parts, "text": failure_sentence(parts)}
        return []

    def connected(self, token, name, row):
        """A connection to ``row`` worked: clear the failure, log a recovery line."""
        now = self._clock()
        with self._lock:
            entry = self._entry(token)
            failed = entry["failed_total"]
            since = entry["failing_since"]
            entry["last_ok"] = {"host": row["host"], "port": row["port"],
                                "via": row.get("via") or classify_via(row["host"]), "at": now}
            entry["failure"] = None
            entry["failing_since"] = None
            entry["streaks"] = {}
            entry["failed_rounds"] = 0
            entry["failed_total"] = 0
        text = f"peer {name}: connected via {_address_label(row)}"
        if failed:
            text += f" after {failed} failed attempt(s) since {_clock_text(since or now)}"
        return [("INFO", text)]

    def disconnected(self, token, reason=""):
        """The link ended: the next round starts a fresh failure time."""
        with self._lock:
            entry = self._entry(token)
            if entry["failing_since"] is None:
                entry["failing_since"] = self._clock()
            entry["disconnect_reason"] = str(reason or "")[:120]

    def forget(self, token):
        with self._lock:
            self._peers.pop(token, None)

    def snapshot(self, token):
        """``{"last_ok", "failure", "failing_since"}`` for the status API (copies)."""
        with self._lock:
            entry = self._peers.get(token)
            if entry is None:
                return {"last_ok": None, "failure": None, "failing_since": None}
            failure = entry["failure"]
            return {
                "last_ok": dict(entry["last_ok"]) if entry["last_ok"] else None,
                "failure": ({"at": failure["at"], "text": failure["text"],
                             "tried": [dict(t) for t in failure["tried"]]} if failure else None),
                "failing_since": entry["failing_since"],
            }

    def sentence(self, token):
        """One sentence why the peer is not connected now, or ``""``."""
        snap = self.snapshot(token)
        failure = snap["failure"]
        if not failure:
            return ""
        return f"Not connected since {_clock_text(snap['failing_since'] or failure['at'])}: " \
               f"{failure['text']}"


def failure_sentence(tried):
    """``No answer from 192.168.8.18 (LAN) and 100.64.1.2 (Tailscale): ...``."""
    if not tried:
        return "no address known for this PC."
    by_reason = {}
    for row in tried:
        by_reason.setdefault(row.get("reason") or REASON_ERROR, []).append(
            f"{row['host']} ({via_label(row.get('via'))})")
    parts = []
    for code, hosts in by_reason.items():
        joined = hosts[0] if len(hosts) == 1 else ", ".join(hosts[:-1]) + " and " + hosts[-1]
        parts.append(f"{joined}: {reason_text(code)}")
    return "; ".join(parts) + "."
