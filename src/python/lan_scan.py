"""Broadcast scan for FlowShift PCs in the local networks, one socket per adapter.

A single socket bound to ``0.0.0.0`` lets Windows send the broadcast out of
the adapter with the lowest metric only (often WireGuard or Tailscale on a
laptop), and the old code assumed a /24 network for every address. Here every
IPv4 adapter gets its own socket bound to its address, sending to the real
broadcast address of its network (from the prefix length) and to
255.255.255.255, which Windows sends out of the bound adapter.

The reply to a random source port can be dropped by the Windows Firewall
(depending on the profile). The request therefore carries ``reply_port``
(the FlowShift port, 45781 by default): an answering PC sends its reply to
the source port and also to that port. The installer allows UDP 45781
inbound for all profiles, and the discovery listener of this PC hands such
replies to :data:`INBOX` while a scan runs.
"""

import ipaddress
import json
import socket
import threading
import time

DEFAULT_TIMEOUT = 3.0
LIMITED_BROADCAST = "255.255.255.255"
ASSUMED_PREFIX = 24

STATUS_SENT = "sent"
STATUS_POINT_TO_POINT = "point_to_point"
STATUS_FAILED = "failed"


def network_entry(address, prefix=None, name=""):
    """One adapter network: address, prefix, broadcast address, adapter name.

    ``prefix`` None (unknown mask) assumes /24 and says so in ``mask_known``.
    Returns None for loopback, link-local and invalid addresses.
    """
    try:
        ip = ipaddress.IPv4Address(str(address).strip())
    except (ipaddress.AddressValueError, ValueError):
        return None
    if ip.is_loopback or ip.is_link_local or ip.is_unspecified:
        return None
    mask_known = True
    try:
        prefix = int(prefix)
        if not 0 < prefix <= 32:
            raise ValueError(prefix)
    except (TypeError, ValueError):
        prefix = ASSUMED_PREFIX
        mask_known = False
    net = ipaddress.IPv4Network(f"{ip}/{prefix}", strict=False)
    # /31 and /32 are point-to-point links (Tailscale, many WireGuard setups):
    # nothing to broadcast to.
    broadcast = str(net.broadcast_address) if prefix <= 30 else None
    return {
        "name": str(name or "").strip(),
        "address": str(ip),
        "prefix": prefix,
        "mask_known": mask_known,
        "broadcast": broadcast,
    }


def adapter_networks(items):
    """Normalise adapters (dicts with address/prefix/name or plain IP strings)."""
    out = []
    seen = set()
    for item in items or ():
        if isinstance(item, dict):
            entry = network_entry(item.get("address") or item.get("IPAddress"),
                                  item.get("prefix", item.get("PrefixLength")),
                                  item.get("name") or item.get("InterfaceAlias") or "")
        else:
            entry = network_entry(item)
        if entry is None or entry["address"] in seen:
            continue
        seen.add(entry["address"])
        out.append(entry)
    return out


def parse_netipaddress(text):
    """Adapters from ``Get-NetIPAddress ... | Select IPAddress,PrefixLength,InterfaceAlias | ConvertTo-Json``."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    return adapter_networks([d for d in data if isinstance(d, dict)])


def build_request(reply_port):
    return json.dumps({"type": "discover", "reply_port": int(reply_port)}).encode("utf-8")


def reply_targets(addr, request):
    """Where an answering PC sends its ``discover_reply``.

    Always the source address; with a valid ``reply_port`` also that port on
    the same host (never another host, so nobody can redirect replies).
    """
    targets = [tuple(addr)]
    port = request.get("reply_port") if isinstance(request, dict) else None
    if isinstance(port, int) and not isinstance(port, bool) and 1024 <= port <= 65535:
        extra = (addr[0], port)
        if extra not in targets:
            targets.append(extra)
    return targets


class ReplyInbox:
    """Replies that arrived on the FlowShift port, handed over to running scans."""

    def __init__(self):
        self._lock = threading.Lock()
        self._collectors = []

    def open(self):
        box = []
        with self._lock:
            self._collectors.append(box)
        return box

    def close(self, box):
        with self._lock:
            if box in self._collectors:
                self._collectors.remove(box)

    def deliver(self, reply, addr):
        """Give ``reply`` to every running scan; False when no scan runs."""
        with self._lock:
            for box in self._collectors:
                box.append((reply, tuple(addr)))
            return bool(self._collectors)


INBOX = ReplyInbox()


def _adapter_label(adapter):
    name = adapter.get("name") or "adapter"
    return f"{name} ({adapter['address']}/{adapter['prefix']})"


def _open_socket(factory, address):
    sock = factory(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind((address, 0))
        sock.settimeout(0.0)
    except Exception:
        sock.close()
        raise
    return sock


def scan(port, adapters, timeout=DEFAULT_TIMEOUT, local_device_id="",
         socket_factory=socket.socket, inbox=None, clock=time.monotonic,
         sleep=time.sleep, unicast=()):
    """Ask every adapter network for FlowShift PCs.

    ``unicast`` are ``{"host", "via"}`` (Tailscale devices, VPN network
    addresses): the same request sent directly from one extra socket,
    because broadcasts do not pass a VPN tunnel.

    Returns ``(found, searched)``: ``found`` are ``{name, host, port,
    device_id, adapter, via}`` (each PC once, own device skipped, ``via``
    ``broadcast`` or the way of the asked address), ``searched`` lists
    every adapter with its broadcast address and ``status``.
    """
    inbox = INBOX if inbox is None else inbox
    msg = build_request(port)
    local_id = str(local_device_id or "").strip().lower()
    sockets = []
    searched = []
    via_by_host = {}
    for target in unicast or ():
        host = target.get("host") if isinstance(target, dict) else None
        if host and host not in via_by_host:
            via_by_host[host] = target.get("via") or "vpn"
    if via_by_host:
        try:
            sock = _open_socket(socket_factory, "0.0.0.0")
        except Exception:
            sock = None
        if sock is not None:
            sent = 0
            for host in via_by_host:
                try:
                    sock.sendto(msg, (host, port))
                    sent += 1
                except Exception:
                    pass
            if sent:
                sockets.append((sock, {"label": "direct"}))
            else:
                sock.close()
    for adapter in adapters_or_default(adapters):
        row = dict(adapter)
        row["label"] = _adapter_label(adapter)
        targets = [t for t in (adapter.get("broadcast"), LIMITED_BROADCAST) if t]
        if adapter.get("broadcast") is None and adapter.get("address") != "0.0.0.0":
            # Point-to-point: no broadcast network behind it.
            row["status"] = STATUS_POINT_TO_POINT
            searched.append(row)
            continue
        try:
            sock = _open_socket(socket_factory, adapter["address"])
        except Exception as e:
            row["status"] = STATUS_FAILED
            row["error"] = str(e)
            searched.append(row)
            continue
        sent = 0
        for target in targets:
            try:
                sock.sendto(msg, (target, port))
                sent += 1
            except Exception as e:
                row["error"] = str(e)
        row["status"] = STATUS_SENT if sent else STATUS_FAILED
        searched.append(row)
        if sent:
            sockets.append((sock, row))
        else:
            sock.close()

    found = []
    seen = set()

    def take(reply, addr, via):
        if not isinstance(reply, dict) or reply.get("type") != "discover_reply":
            return
        remote_id = str(reply.get("device_id", "") or "").strip().lower()
        if remote_id and local_id and remote_id == local_id:
            return
        key = remote_id or addr[0]
        if key in seen:
            return
        seen.add(key)
        reply_port = reply.get("port", port)
        found.append({
            "name": reply.get("display_name", ""),
            "host": addr[0],
            "port": reply_port if isinstance(reply_port, int) else port,
            "device_id": remote_id,
            "adapter": via,
            "via": via_by_host.get(addr[0], "broadcast"),
        })

    box = inbox.open()
    try:
        deadline = clock() + max(0.1, float(timeout))
        while True:
            for sock, row in sockets:
                while True:
                    try:
                        data, addr = sock.recvfrom(4096)
                    except (BlockingIOError, socket.timeout):
                        break
                    except OSError:
                        # Windows reports an ICMP "port unreachable" as
                        # ConnectionResetError on the next receive.
                        break
                    try:
                        take(json.loads(data.decode("utf-8")), addr, row["label"])
                    except (ValueError, UnicodeDecodeError):
                        continue
            while box:
                reply, addr = box.pop(0)
                take(reply, addr, _via_for(addr[0], searched))
            if clock() >= deadline:
                break
            sleep(0.03)
    finally:
        inbox.close(box)
        for sock, _ in sockets:
            try:
                sock.close()
            except Exception:
                pass
    return found, searched


def adapters_or_default(adapters):
    """The adapters, or one wildcard entry when none is known."""
    adapters = list(adapters or ())
    if adapters:
        return adapters
    return [{"name": "any", "address": "0.0.0.0", "prefix": 0, "mask_known": False,
             "broadcast": None}]


def _via_for(host, searched):
    """Label of the adapter whose network contains ``host`` (reply on the FlowShift port)."""
    try:
        ip = ipaddress.IPv4Address(host)
    except (ipaddress.AddressValueError, ValueError):
        return ""
    for row in searched:
        try:
            net = ipaddress.IPv4Network(f"{row['address']}/{row['prefix']}", strict=False)
        except (KeyError, ValueError):
            continue
        if row.get("prefix") and ip in net:
            return row.get("label", "")
    return ""


def summary(searched):
    """Short line for the WebGUI: which adapters were searched."""
    used = [r["label"] for r in searched if r.get("status") == STATUS_SENT]
    return ", ".join(used)
